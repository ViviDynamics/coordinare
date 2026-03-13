#!/usr/bin/env -S npx -y tsx

import { createHash, randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { AddressInfo, createConnection, createServer, Socket } from "node:net";

type JsonMap = Record<string, unknown>;

class SimpleWebSocket {
  private socket: Socket;
  private buffer = Buffer.alloc(0);
  private messages: string[] = [];
  private waiters: Array<(value: string) => void> = [];

  private constructor(socket: Socket) {
    this.socket = socket;
    this.socket.on("data", (chunk) => this.onData(chunk));
  }

  static async connect(urlString: string): Promise<SimpleWebSocket> {
    const url = new URL(urlString);
    if (url.protocol !== "ws:") {
      throw new Error(`unsupported protocol: ${url.protocol}`);
    }

    const host = url.hostname;
    const port = Number(url.port || 80);
    const path = `${url.pathname || "/"}${url.search || ""}`;

    const socket = createConnection({ host, port });
    await new Promise<void>((resolve, reject) => {
      socket.once("connect", () => resolve());
      socket.once("error", reject);
    });

    const key = randomBytes(16).toString("base64");
    const request =
      `GET ${path || "/"} HTTP/1.1\r\n` +
      `Host: ${host}:${port}\r\n` +
      `Upgrade: websocket\r\n` +
      `Connection: Upgrade\r\n` +
      `Sec-WebSocket-Key: ${key}\r\n` +
      `Sec-WebSocket-Version: 13\r\n\r\n`;

    socket.write(request);

    let handshake = Buffer.alloc(0);
    const headerEnd = await new Promise<number>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("websocket handshake timed out")), 10_000);
      const onData = (chunk: Buffer) => {
        handshake = Buffer.concat([handshake, chunk]);
        const idx = handshake.indexOf("\r\n\r\n");
        if (idx !== -1) {
          clearTimeout(timer);
          socket.off("data", onData);
          resolve(idx);
        }
      };
      socket.on("data", onData);
      socket.once("error", (err) => {
        clearTimeout(timer);
        socket.off("data", onData);
        reject(err);
      });
    });

    const headerText = handshake.slice(0, headerEnd).toString("utf8");
    if (!headerText.includes(" 101 ")) {
      throw new Error(`websocket upgrade failed: ${headerText.split("\r\n")[0]}`);
    }

    const acceptLine = headerText
      .split("\r\n")
      .find((line) => line.toLowerCase().startsWith("sec-websocket-accept:"));
    if (!acceptLine) {
      throw new Error("missing Sec-WebSocket-Accept in handshake response");
    }

    const acceptValue = acceptLine.split(":", 2)[1].trim();
    const expectedAccept = createHash("sha1")
      .update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
      .digest("base64");
    if (acceptValue !== expectedAccept) {
      throw new Error("invalid Sec-WebSocket-Accept value");
    }

    const ws = new SimpleWebSocket(socket);
    const leftover = handshake.slice(headerEnd + 4);
    if (leftover.length > 0) {
      ws.onData(leftover);
    }
    return ws;
  }

  sendText(text: string): void {
    const payload = Buffer.from(text, "utf8");
    const header: number[] = [0x81];
    const maskBit = 0x80;

    if (payload.length < 126) {
      header.push(maskBit | payload.length);
    } else if (payload.length <= 0xffff) {
      header.push(maskBit | 126, (payload.length >> 8) & 0xff, payload.length & 0xff);
    } else {
      const len = BigInt(payload.length);
      header.push(maskBit | 127);
      for (let i = 7; i >= 0; i -= 1) {
        header.push(Number((len >> BigInt(i * 8)) & 0xffn));
      }
    }

    const mask = randomBytes(4);
    const masked = Buffer.alloc(payload.length);
    for (let i = 0; i < payload.length; i += 1) {
      masked[i] = payload[i] ^ mask[i % 4];
    }

    this.socket.write(Buffer.concat([Buffer.from(header), mask, masked]));
  }

  async recvText(): Promise<string> {
    if (this.messages.length > 0) {
      return this.messages.shift() as string;
    }
    return new Promise<string>((resolve) => this.waiters.push(resolve));
  }

  close(): void {
    try {
      this.socket.end();
    } catch {
      // best effort
    }
  }

  private onData(chunk: Buffer): void {
    this.buffer = Buffer.concat([this.buffer, chunk]);

    while (true) {
      if (this.buffer.length < 2) return;

      const b0 = this.buffer[0];
      const b1 = this.buffer[1];
      const opcode = b0 & 0x0f;
      const masked = (b1 & 0x80) !== 0;
      let length = b1 & 0x7f;
      let offset = 2;

      if (length === 126) {
        if (this.buffer.length < offset + 2) return;
        length = this.buffer.readUInt16BE(offset);
        offset += 2;
      } else if (length === 127) {
        if (this.buffer.length < offset + 8) return;
        const lenBig = this.buffer.readBigUInt64BE(offset);
        if (lenBig > BigInt(Number.MAX_SAFE_INTEGER)) {
          throw new Error("frame too large");
        }
        length = Number(lenBig);
        offset += 8;
      }

      let mask: Buffer | null = null;
      if (masked) {
        if (this.buffer.length < offset + 4) return;
        mask = this.buffer.slice(offset, offset + 4);
        offset += 4;
      }

      if (this.buffer.length < offset + length) return;

      let payload = this.buffer.slice(offset, offset + length);
      this.buffer = this.buffer.slice(offset + length);

      if (mask) {
        const unmasked = Buffer.alloc(payload.length);
        for (let i = 0; i < payload.length; i += 1) {
          unmasked[i] = payload[i] ^ mask[i % 4];
        }
        payload = unmasked;
      }

      if (opcode === 0x1) {
        const text = payload.toString("utf8");
        const waiter = this.waiters.shift();
        if (waiter) waiter(text);
        else this.messages.push(text);
      } else if (opcode === 0x8) {
        this.socket.end();
        return;
      } else if (opcode === 0x9) {
        this.sendControlFrame(0xA, payload); // pong
      }
    }
  }

  private sendControlFrame(opcode: number, payload: Buffer): void {
    const frame = Buffer.concat([Buffer.from([0x80 | opcode, payload.length]), payload]);
    this.socket.write(frame);
  }
}

function pickPort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address() as AddressInfo;
      server.close((err) => {
        if (err) reject(err);
        else resolve(address.port);
      });
    });
  });
}

function waitForListeningLine(serverProcess: ReturnType<typeof spawn>, timeoutMs = 10_000): Promise<void> {
  const readers = [];
  if (serverProcess.stdout) readers.push(createInterface({ input: serverProcess.stdout }));
  if (serverProcess.stderr) readers.push(createInterface({ input: serverProcess.stderr }));
  if (readers.length === 0) {
    return Promise.reject(new Error("codex app-server output streams are unavailable"));
  }

  return new Promise((resolve, reject) => {
    const cleanup = () => {
      for (const reader of readers) reader.close();
    };

    const timer = setTimeout(() => {
      cleanup();
      reject(new Error("timed out waiting for codex app-server startup"));
    }, timeoutMs);

    for (const reader of readers) {
      reader.on("line", (line) => {
        if (line.includes("listening on:")) {
          clearTimeout(timer);
          cleanup();
          resolve();
        }
      });
    }

    serverProcess.once("exit", (code) => {
      clearTimeout(timer);
      cleanup();
      reject(new Error(`codex app-server exited early (code=${String(code)})`));
    });
  });
}

async function recvJson(ws: SimpleWebSocket): Promise<JsonMap> {
  const raw = await ws.recvText();
  return JSON.parse(raw) as JsonMap;
}

function sendJson(ws: SimpleWebSocket, payload: JsonMap): void {
  ws.sendText(JSON.stringify(payload));
}

async function request(ws: SimpleWebSocket, id: string, method: string, params?: JsonMap): Promise<JsonMap> {
  const payload: JsonMap = { id, method };
  if (params) payload.params = params;
  sendJson(ws, payload);

  while (true) {
    const msg = await recvJson(ws);
    if (String(msg.id ?? "") === id) {
      if (msg.error) {
        throw new Error(`RPC error (${id}): ${JSON.stringify(msg.error)}`);
      }
      return msg;
    }
  }
}

async function main(): Promise<void> {
  const prompt = process.argv.slice(2).join(" ").trim() || "Say OK and nothing else.";
  const port = await pickPort();

  const serverProcess = spawn("codex", ["app-server", "--listen", `ws://127.0.0.1:${port}`], {
    stdio: ["ignore", "pipe", "pipe"],
  });

  if (serverProcess.stderr) {
    serverProcess.stderr.on("data", (chunk) => {
      const text = chunk.toString();
      if (text.trim().length) process.stderr.write(`[app-server] ${text}`);
    });
  }

  let ws: SimpleWebSocket | null = null;
  try {
    await waitForListeningLine(serverProcess);
    ws = await SimpleWebSocket.connect(`ws://127.0.0.1:${port}`);

    await request(ws, "1", "initialize", {
      clientInfo: { name: "example-typescript-client", version: "0.1.0" },
    });
    sendJson(ws, { method: "initialized" });

    const threadResp = await request(ws, "2", "thread/start", { ephemeral: true });
    const threadId = ((threadResp.result as JsonMap).thread as JsonMap).id as string;

    await request(ws, "3", "turn/start", {
      threadId,
      input: [{ type: "text", text: prompt, text_elements: [] }],
    });

    while (true) {
      const msg = await recvJson(ws);
      const method = String(msg.method ?? "");
      const params = (msg.params as JsonMap | undefined) ?? {};

      if (method === "item/agentMessage/delta") {
        const delta = String(params.delta ?? "");
        if (delta.length) process.stdout.write(delta);
      } else if (method === "item/commandExecution/outputDelta") {
        const delta = String(params.delta ?? "");
        if (delta.length) process.stderr.write(`\n[command] ${delta}`);
      } else if (method === "item/fileChange/outputDelta") {
        const delta = String(params.delta ?? "");
        if (delta.length) process.stderr.write(`\n[file-change] ${delta}`);
      } else if (method === "turn/completed") {
        process.stdout.write("\n");
        break;
      } else if (method === "error") {
        throw new Error(`server error notification: ${JSON.stringify(msg)}`);
      }
    }
  } finally {
    if (ws) ws.close();
    serverProcess.kill("SIGTERM");
    await new Promise<void>((resolve) => {
      serverProcess.once("exit", () => resolve());
      setTimeout(resolve, 2000);
    });
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
