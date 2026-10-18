use anyhow::{Context, Result, bail};
use rusqlite::{Connection, params};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::Path;

#[derive(Clone, Debug)]
pub struct Span {
    pub trace_id: String,
    pub span_id: String,
    pub parent_span_id: Option<String>,
    pub name: String,
    pub start_ns: i64,
    pub end_ns: i64,
    pub status: String,
    pub service: String,
    pub attributes: Value,
    pub events: Value,
    pub links: Value,
    pub resource_attributes: Value,
    pub scope_name: String,
}

fn validated_id(value: Option<&Value>, length: usize, label: &str) -> Result<String> {
    let value = value
        .and_then(Value::as_str)
        .context(format!("invalid {label}"))?;
    if value.len() != length
        || !value.bytes().all(|b| b.is_ascii_hexdigit())
        || value.bytes().all(|b| b == b'0')
    {
        bail!("invalid {label}");
    }
    Ok(value.to_ascii_lowercase())
}

fn timestamp(value: Option<&Value>, label: &str) -> Result<i64> {
    let value = value.context(format!("missing {label}"))?;
    value
        .as_i64()
        .or_else(|| value.as_str().and_then(|s| s.parse().ok()))
        .context(format!("invalid {label}"))
}

pub fn normalize_native(raw: &Value, service: &str) -> Result<Span> {
    let trace_id = validated_id(raw.get("trace_id"), 32, "trace_id")?;
    let span_id = validated_id(raw.get("span_id"), 16, "span_id")?;
    let parent_span_id = match raw.get("parent_span_id") {
        None | Some(Value::Null) | Some(Value::String(_))
            if raw
                .get("parent_span_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .is_empty() =>
        {
            None
        }
        value => Some(validated_id(value, 16, "parent_span_id")?),
    };
    let start_ns = timestamp(raw.get("start_unix_nano"), "start_unix_nano")?;
    let end_ns = timestamp(raw.get("end_unix_nano"), "end_unix_nano")?;
    if start_ns < 0 || end_ns < start_ns {
        bail!("invalid span timestamps");
    }
    let attributes = raw
        .get("attributes")
        .filter(|v| !v.is_null())
        .cloned()
        .unwrap_or_else(|| json!({}));
    let events = raw
        .get("events")
        .filter(|v| !v.is_null())
        .cloned()
        .unwrap_or_else(|| json!([]));
    let links = raw
        .get("links")
        .filter(|v| !v.is_null())
        .cloned()
        .unwrap_or_else(|| json!([]));
    let resource_attributes = raw
        .get("resource_attributes")
        .filter(|v| !v.is_null())
        .cloned()
        .unwrap_or_else(|| json!({}));
    if !attributes.is_object()
        || !resource_attributes.is_object()
        || !events.is_array()
        || !links.is_array()
    {
        bail!("attributes/resource must be objects and events/links arrays");
    }
    let links: Vec<Value> = links.as_array().unwrap().iter().map(|link| {
        Ok(json!({
            "trace_id": validated_id(link.get("trace_id"), 32, "link trace_id")?,
            "span_id": validated_id(link.get("span_id"), 16, "link span_id")?,
            "attributes": link.get("attributes").filter(|v| !v.is_null()).cloned().unwrap_or_else(|| json!({}))
        }))
    }).collect::<Result<_>>()?;
    Ok(Span {
        trace_id,
        span_id,
        parent_span_id,
        name: raw
            .get("name")
            .and_then(Value::as_str)
            .context("missing name")?
            .to_string(),
        start_ns,
        end_ns,
        status: raw
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("UNSET")
            .to_string(),
        service: service.to_string(),
        attributes,
        events,
        links: json!(links),
        resource_attributes,
        scope_name: raw
            .get("scope_name")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
    })
}

fn attributes(items: Option<&Value>) -> Result<Value> {
    let mut result = Map::new();
    for item in items.and_then(Value::as_array).into_iter().flatten() {
        let key = item
            .get("key")
            .and_then(Value::as_str)
            .context("missing attribute key")?;
        result.insert(
            key.to_string(),
            any_value(item.get("value").unwrap_or(&Value::Null))?,
        );
    }
    Ok(Value::Object(result))
}

fn any_value(value: &Value) -> Result<Value> {
    if let Some(array) = value.get("arrayValue") {
        return Ok(Value::Array(
            array
                .get("values")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .map(any_value)
                .collect::<Result<_>>()?,
        ));
    }
    if let Some(kv) = value.get("kvlistValue") {
        return attributes(kv.get("values"));
    }
    for key in [
        "stringValue",
        "intValue",
        "doubleValue",
        "boolValue",
        "bytesValue",
    ] {
        if let Some(found) = value.get(key) {
            return if key == "intValue" && found.is_string() {
                Ok(json!(found.as_str().unwrap().parse::<i64>()?))
            } else {
                Ok(found.clone())
            };
        }
    }
    Ok(Value::Null)
}

pub fn normalize_otlp(body: &Value) -> Result<Vec<Span>> {
    let mut spans = Vec::new();
    for group in body
        .get("resourceSpans")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let resource = attributes(group.get("resource").and_then(|v| v.get("attributes")))?;
        let service = resource
            .get("service.name")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        for scope in group
            .get("scopeSpans")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let scope_name = scope
                .get("scope")
                .and_then(|v| v.get("name"))
                .and_then(Value::as_str)
                .unwrap_or("");
            for raw in scope
                .get("spans")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                let events = raw.get("events").and_then(Value::as_array).into_iter().flatten()
                    .map(|event| Ok(json!({
                        "name": event.get("name").and_then(Value::as_str).unwrap_or("event"),
                        "time_unix_nano": timestamp(event.get("timeUnixNano").or(Some(&json!(0))), "event time")?,
                        "attributes": attributes(event.get("attributes"))?
                    }))).collect::<Result<Vec<_>>>()?;
                let links = raw
                    .get("links")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .map(|link| {
                        Ok(json!({
                            "trace_id": link.get("traceId"), "span_id": link.get("spanId"),
                            "attributes": attributes(link.get("attributes"))?
                        }))
                    })
                    .collect::<Result<Vec<_>>>()?;
                let status_code = raw
                    .get("status")
                    .and_then(|s| s.get("code"))
                    .and_then(|v| {
                        v.as_i64()
                            .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
                    })
                    .unwrap_or(0);
                let native = json!({
                    "trace_id": raw.get("traceId"), "span_id": raw.get("spanId"),
                    "parent_span_id": raw.get("parentSpanId"),
                    "name": raw.get("name").and_then(Value::as_str).unwrap_or("unnamed"),
                    "start_unix_nano": raw.get("startTimeUnixNano"),
                    "end_unix_nano": raw.get("endTimeUnixNano"),
                    "status": match status_code { 1 => "OK", 2 => "ERROR", _ => "UNSET" },
                    "attributes": attributes(raw.get("attributes"))?,
                    "resource_attributes": resource, "scope_name": scope_name,
                    "events": events, "links": links
                });
                spans.push(normalize_native(&native, service)?);
            }
        }
    }
    Ok(spans)
}

pub fn open_db(path: &Path) -> Result<Connection> {
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent)?;
    }
    let db = Connection::open(path)?;
    db.busy_timeout(std::time::Duration::from_secs(10))?;
    db.execute_batch("CREATE TABLE IF NOT EXISTS spans (
        trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT,
        name TEXT NOT NULL, start_ns INTEGER NOT NULL, end_ns INTEGER NOT NULL,
        status TEXT NOT NULL, service TEXT NOT NULL, attributes TEXT NOT NULL,
        events TEXT NOT NULL, links TEXT NOT NULL DEFAULT '[]',
        resource_attributes TEXT NOT NULL DEFAULT '{}', scope_name TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(trace_id, span_id));
        CREATE INDEX IF NOT EXISTS spans_time ON spans(end_ns DESC);
        CREATE INDEX IF NOT EXISTS spans_service_time ON spans(service,end_ns);
        CREATE TABLE IF NOT EXISTS trace_activity (trace_id TEXT PRIMARY KEY, last_ns INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS trace_activity_time ON trace_activity(last_ns DESC,trace_id DESC);
        CREATE TABLE IF NOT EXISTS endpoints (
            endpoint_id TEXT PRIMARY KEY, identity_key TEXT NOT NULL,
            identity_value TEXT NOT NULL, resource_attributes TEXT NOT NULL,
            last_ns INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS endpoint_services (
            endpoint_id TEXT NOT NULL, service TEXT NOT NULL, last_ns INTEGER NOT NULL,
            PRIMARY KEY(endpoint_id,service));
        CREATE TABLE IF NOT EXISTS endpoint_traces (
            endpoint_id TEXT NOT NULL, trace_id TEXT NOT NULL, last_ns INTEGER NOT NULL,
            PRIMARY KEY(endpoint_id,trace_id));
        CREATE INDEX IF NOT EXISTS endpoint_traces_recent ON endpoint_traces(endpoint_id,last_ns DESC);
        CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT OR IGNORE INTO schema_meta(key,value) VALUES('trace_activity_v1','1');")?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    }
    Ok(db)
}

pub fn ingest(db: &mut Connection, spans: &[Span]) -> Result<usize> {
    if spans.is_empty() {
        return Ok(0);
    }
    let tx = db.transaction()?;
    let mut touched = HashSet::new();
    {
        let mut insert = tx.prepare_cached("INSERT INTO spans
            (trace_id,span_id,parent_span_id,name,start_ns,end_ns,status,service,attributes,events,links,resource_attributes,scope_name)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trace_id,span_id) DO UPDATE SET
            parent_span_id=excluded.parent_span_id,name=excluded.name,start_ns=excluded.start_ns,
            end_ns=excluded.end_ns,status=excluded.status,service=excluded.service,
            attributes=excluded.attributes,events=excluded.events,links=excluded.links,
            resource_attributes=excluded.resource_attributes,scope_name=excluded.scope_name")?;
        let mut touch = tx.prepare_cached(
            "INSERT INTO trace_activity(trace_id,last_ns) VALUES(?,?)
            ON CONFLICT(trace_id) DO UPDATE SET last_ns=MAX(last_ns,excluded.last_ns)",
        )?;
        let mut endpoint = tx.prepare_cached("INSERT INTO endpoints(endpoint_id,identity_key,identity_value,last_ns,resource_attributes)
            VALUES(?,?,?,?,?) ON CONFLICT(endpoint_id) DO UPDATE SET
            last_ns=MAX(last_ns,excluded.last_ns),
            resource_attributes=CASE WHEN excluded.last_ns>=last_ns
                THEN excluded.resource_attributes ELSE resource_attributes END")?;
        let mut endpoint_service = tx.prepare_cached("INSERT INTO endpoint_services(endpoint_id,service,last_ns)
            VALUES(?,?,?) ON CONFLICT(endpoint_id,service) DO UPDATE SET last_ns=MAX(last_ns,excluded.last_ns)")?;
        let mut endpoint_trace = tx.prepare_cached("INSERT INTO endpoint_traces(endpoint_id,trace_id,last_ns)
            VALUES(?,?,?) ON CONFLICT(endpoint_id,trace_id) DO UPDATE SET last_ns=MAX(last_ns,excluded.last_ns)")?;
        for span in spans {
            insert.execute(params![
                span.trace_id,
                span.span_id,
                span.parent_span_id,
                span.name,
                span.start_ns,
                span.end_ns,
                span.status,
                span.service,
                span.attributes.to_string(),
                span.events.to_string(),
                span.links.to_string(),
                span.resource_attributes.to_string(),
                span.scope_name
            ])?;
            touch.execute(params![span.trace_id, span.end_ns])?;
            if let Some((key, value)) = endpoint_identity(&span.resource_attributes) {
                let digest = Sha256::digest(format!("{key}\0{value}").as_bytes());
                let endpoint_id: String = digest[..16]
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect();
                let mut attrs = Map::new();
                for name in ENDPOINT_RESOURCE_KEYS {
                    if let Some(value) = span.resource_attributes.get(name).filter(|value| {
                        value.is_string() || value.is_number() || value.is_boolean()
                    }) {
                        attrs.insert(name.to_string(), value.clone());
                    }
                }
                endpoint.execute(params![
                    endpoint_id,
                    key,
                    value,
                    span.end_ns,
                    Value::Object(attrs).to_string()
                ])?;
                endpoint_service.execute(params![endpoint_id, span.service, span.end_ns])?;
                endpoint_trace.execute(params![endpoint_id, span.trace_id, span.end_ns])?;
            }
            touched.insert(span.trace_id.as_str());
        }
    }
    // Python's Store recomputes these derived rows on demand after Rust ingestion.
    if table_exists(&tx, "trace_summaries")? {
        for trace_id in &touched {
            tx.execute("DELETE FROM trace_summaries WHERE trace_id=?", [trace_id])?;
        }
    }
    if table_exists(&tx, "finding_cache")? {
        for trace_id in &touched {
            tx.execute("DELETE FROM finding_cache WHERE trace_id=?", [trace_id])?;
        }
    }
    tx.commit()?;
    Ok(spans.len())
}

const ENDPOINT_RESOURCE_KEYS: &[&str] = &[
    "host.id",
    "host.name",
    "device.id",
    "service.instance.id",
    "host.arch",
    "os.type",
    "os.name",
    "os.description",
    "os.version",
    "user.name",
    "user.id",
    "deployment.environment.name",
    "cloud.region",
];

fn endpoint_identity(resource: &Value) -> Option<(&'static str, String)> {
    for key in ["host.id", "host.name", "device.id", "service.instance.id"] {
        if let Some(value) = resource.get(key).and_then(Value::as_str) {
            let value = value.trim();
            if !value.is_empty() {
                return Some((key, value.chars().take(512).collect()));
            }
        }
    }
    None
}

fn table_exists(db: &Connection, name: &str) -> Result<bool> {
    Ok(db.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)",
        [name],
        |row| row.get::<_, bool>(0),
    )?)
}

pub fn import_jsonl(db: &mut Connection, path: &Path, service: &str) -> Result<usize> {
    let file = File::open(path).with_context(|| format!("opening {}", path.display()))?;
    let reader = BufReader::new(file);
    let mut batch = Vec::with_capacity(1000);
    let mut accepted = 0;
    for (index, line) in reader.lines().enumerate() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let raw: Value =
            serde_json::from_str(&line).with_context(|| format!("line {}", index + 1))?;
        batch.push(normalize_native(&raw, service).with_context(|| format!("line {}", index + 1))?);
        if batch.len() == 1000 {
            accepted += ingest(db, &batch)?;
            batch.clear();
        }
    }
    accepted += ingest(db, &batch)?;
    Ok(accepted)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> Value {
        json!({"trace_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "span_id":"1111111111111111",
            "name":"agent.run", "start_unix_nano":100, "end_unix_nano":200,
            "attributes":{"agent.task":"test"}, "events":[]})
    }

    #[test]
    fn native_rejects_invalid_ids_and_timestamps() {
        let mut raw = sample();
        assert!(normalize_native(&raw, "test").is_ok());
        raw["trace_id"] = json!("00000000000000000000000000000000");
        assert!(normalize_native(&raw, "test").is_err());
        raw = sample();
        raw["end_unix_nano"] = json!(99);
        assert!(normalize_native(&raw, "test").is_err());
    }

    #[test]
    fn ingest_deduplicates_in_compatible_schema() -> Result<()> {
        let mut db = Connection::open_in_memory()?;
        db.execute_batch(
            "CREATE TABLE spans(trace_id TEXT, span_id TEXT, parent_span_id TEXT, name TEXT,
            start_ns INTEGER,end_ns INTEGER,status TEXT,service TEXT,attributes TEXT,events TEXT,
            links TEXT,resource_attributes TEXT,scope_name TEXT,PRIMARY KEY(trace_id,span_id));
            CREATE TABLE trace_activity(trace_id TEXT PRIMARY KEY,last_ns INTEGER);
            CREATE TABLE endpoints(endpoint_id TEXT PRIMARY KEY,identity_key TEXT,identity_value TEXT,
                last_ns INTEGER,resource_attributes TEXT);
            CREATE TABLE endpoint_services(endpoint_id TEXT,service TEXT,last_ns INTEGER,
                PRIMARY KEY(endpoint_id,service));
            CREATE TABLE endpoint_traces(endpoint_id TEXT,trace_id TEXT,last_ns INTEGER,
                PRIMARY KEY(endpoint_id,trace_id));",
        )?;
        let span = normalize_native(&sample(), "test")?;
        ingest(&mut db, &[span.clone(), span])?;
        let count: i64 = db.query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))?;
        assert_eq!(count, 1);
        Ok(())
    }

    #[test]
    fn otlp_json_normalizes_resource_and_status() -> Result<()> {
        let body = json!({"resourceSpans":[{"resource":{"attributes":[{"key":"service.name",
            "value":{"stringValue":"demo"}}]},"scopeSpans":[{"spans":[{"traceId":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "spanId":"1111111111111111","name":"model.call","startTimeUnixNano":"100",
            "endTimeUnixNano":"200","status":{"code":2}}]}]}]});
        let spans = normalize_otlp(&body)?;
        assert_eq!(spans[0].service, "demo");
        assert_eq!(spans[0].status, "ERROR");
        Ok(())
    }
}
