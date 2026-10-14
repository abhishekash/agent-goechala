export interface ClientOptions {
  endpoint?: string;
  service?: string;
  token?: string;
  timeoutMs?: number;
  strict?: boolean;
  resourceAttributes?: Record<string, unknown>;
}

export interface TraceOptions {
  name?: string;
  task?: string;
  sessionId?: string;
  attributes?: Record<string, unknown>;
}

export interface SpanOptions {
  attributes?: Record<string, unknown>;
}

export interface SpanHandle {
  readonly span_id: string;
  readonly parent_span_id: string | null;
  setAttribute(key: string, value: unknown): this;
  addEvent(name: string, attributes?: Record<string, unknown>): this;
  setStatus(status: 'UNSET' | 'OK' | 'ERROR'): this;
}

export interface RunHandle {
  readonly trace_id: string;
  progress(stage: string, message?: string, percent?: number): Promise<boolean>;
  span<T>(name: string, callback: (span: SpanHandle) => T | Promise<T>): Promise<T>;
  span<T>(name: string, options: SpanOptions, callback: (span: SpanHandle) => T | Promise<T>): Promise<T>;
}

export declare class Client {
  constructor(options?: ClientOptions);
  readonly endpoint: string;
  readonly service: string;
  readonly lastError: Error | null;
  trace<T>(callback: (run: RunHandle) => T | Promise<T>): Promise<T>;
  trace<T>(options: TraceOptions, callback: (run: RunHandle) => T | Promise<T>): Promise<T>;
}
