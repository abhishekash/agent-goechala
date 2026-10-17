use anyhow::{Result, anyhow};
use clap::{Parser, Subcommand};
use flate2::read::GzDecoder;
use serde_json::{Value, json};
use std::io::Read;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use tiny_http::{Header, Method, Request, Response, Server, StatusCode};

const MAX_BODY: u64 = 8 * 1024 * 1024;

#[derive(Parser)]
#[command(about = "Experimental Rust ingestion for Agent Observatory")]
struct Cli {
    #[arg(long, default_value = "observatory.db")]
    db: PathBuf,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    ImportJsonl {
        path: PathBuf,
        #[arg(long, default_value = "agent-harness")]
        service: String,
    },
    Serve {
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
        #[arg(long, default_value_t = 4318)]
        port: u16,
        #[arg(long, env = "OBSERVATORY_TOKEN")]
        token: Option<String>,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::ImportJsonl { path, service } => {
            let mut db = agent_observatory_rs::open_db(&cli.db)?;
            let count = agent_observatory_rs::import_jsonl(&mut db, &path, &service)?;
            println!("Imported {count} spans");
        }
        Command::Serve { host, port, token } => serve(cli.db, host, port, token)?,
    }
    Ok(())
}

fn serve(path: PathBuf, host: String, port: u16, token: Option<String>) -> Result<()> {
    let db = Arc::new(Mutex::new(agent_observatory_rs::open_db(&path)?));
    let server =
        Server::http(format!("{host}:{port}")).map_err(|e| anyhow!("cannot bind receiver: {e}"))?;
    let (sender, receiver) = mpsc::sync_channel::<Request>(128);
    let receiver = Arc::new(Mutex::new(receiver));
    let token = Arc::new(token);
    for _ in 0..4 {
        let db = db.clone();
        let receiver = receiver.clone();
        let token = token.clone();
        thread::spawn(move || {
            loop {
                let request = match receiver.lock().unwrap().recv() {
                    Ok(request) => request,
                    Err(_) => break,
                };
                handle(request, &db, token.as_deref());
            }
        });
    }
    println!(
        "Rust trace receiver listening at http://{host}:{port} (database: {})",
        path.display()
    );
    for request in server.incoming_requests() {
        if sender.send(request).is_err() {
            break;
        }
    }
    Ok(())
}

fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    let mut difference = left.len() ^ right.len();
    for (a, b) in left.iter().zip(right) {
        difference |= usize::from(a ^ b);
    }
    difference == 0
}

fn header<'a>(request: &'a Request, name: &str) -> Option<&'a str> {
    request
        .headers()
        .iter()
        .find(|h| h.field.as_str().as_str().eq_ignore_ascii_case(name))
        .map(|h| h.value.as_str())
}

fn respond(request: Request, code: u16, value: Value) {
    let body = value.to_string();
    let response = Response::from_string(body)
        .with_status_code(StatusCode(code))
        .with_header(Header::from_bytes("Content-Type", "application/json; charset=utf-8").unwrap())
        .with_header(Header::from_bytes("Cache-Control", "no-store").unwrap());
    let _ = request.respond(response);
}

fn handle(mut request: Request, db: &Arc<Mutex<rusqlite::Connection>>, token: Option<&str>) {
    let path = request.url().split('?').next().unwrap_or("").to_string();
    if let Some(token) = token {
        let expected = format!("Bearer {token}");
        let supplied = header(&request, "Authorization").unwrap_or("");
        if !constant_time_equal(supplied.as_bytes(), expected.as_bytes()) {
            respond(request, 401, json!({"error":"unauthorized"}));
            return;
        }
    }
    if request.method() == &Method::Get && path == "/api/health" {
        respond(
            request,
            200,
            json!({"ok":true,"local_agents":{"enabled":false}}),
        );
        return;
    }
    if request.method() != &Method::Post || !matches!(path.as_str(), "/api/spans" | "/v1/traces") {
        respond(request, 404, json!({"error":"not found"}));
        return;
    }
    let content_type = header(&request, "Content-Type")
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim();
    if content_type != "application/json" {
        respond(request, 415, json!({"error":"requires application/json"}));
        return;
    }
    if request
        .body_length()
        .is_some_and(|n| n == 0 || n as u64 > MAX_BODY)
    {
        respond(
            request,
            413,
            json!({"error":"body size must be 1 to 8 MiB"}),
        );
        return;
    }
    let encoding = header(&request, "Content-Encoding")
        .unwrap_or("identity")
        .to_ascii_lowercase();
    if encoding != "identity" && encoding != "gzip" {
        respond(
            request,
            415,
            json!({"error":"unsupported content encoding"}),
        );
        return;
    }
    let mut compressed = Vec::new();
    let result = request
        .as_reader()
        .take(MAX_BODY + 1)
        .read_to_end(&mut compressed);
    if result.is_err() || compressed.is_empty() || compressed.len() as u64 > MAX_BODY {
        respond(
            request,
            413,
            json!({"error":"body size must be 1 to 8 MiB"}),
        );
        return;
    }
    let mut raw = Vec::new();
    if encoding == "gzip" {
        if GzDecoder::new(compressed.as_slice())
            .take(MAX_BODY + 1)
            .read_to_end(&mut raw)
            .is_err()
            || raw.len() as u64 > MAX_BODY
        {
            respond(
                request,
                413,
                json!({"error":"invalid or oversized gzip body"}),
            );
            return;
        }
    } else {
        raw = compressed;
    }
    let parsed: Result<Value> = serde_json::from_slice(&raw).map_err(Into::into);
    let spans = parsed.and_then(|body| {
        if path == "/v1/traces" {
            return agent_observatory_rs::normalize_otlp(&body);
        }
        let service = body
            .get("service")
            .and_then(Value::as_str)
            .unwrap_or("agent-harness");
        let raw_spans = body
            .as_array()
            .or_else(|| body.get("spans").and_then(Value::as_array))
            .ok_or_else(|| anyhow!("spans must be an array"))?;
        raw_spans
            .iter()
            .map(|span| agent_observatory_rs::normalize_native(span, service))
            .collect()
    });
    let spans = match spans {
        Ok(spans) => spans,
        Err(error) => {
            respond(request, 400, json!({"error":error.to_string()}));
            return;
        }
    };
    let count = match agent_observatory_rs::ingest(&mut db.lock().unwrap(), &spans) {
        Ok(count) => count,
        Err(error) => {
            respond(request, 500, json!({"error":error.to_string()}));
            return;
        }
    };
    let value = if path == "/v1/traces" {
        json!({"partialSuccess":{"rejectedSpans":0},"accepted":count})
    } else {
        json!({"accepted":count})
    };
    respond(request, 200, value);
}
