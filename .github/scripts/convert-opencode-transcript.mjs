import { DatabaseSync } from "node:sqlite";
import { homedir } from "node:os";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const PINNED_OPENCODE_SCHEMA = 2;

const dbPath = join(homedir(), ".local", "share", "opencode", "opencode.db");
const db = new DatabaseSync(dbPath, { readOnly: true });

const requestedSessionId = process.argv[2];

let sessionId;
if (requestedSessionId) {
  const row = db
    .prepare("SELECT 1 FROM message WHERE session_id = ? LIMIT 1")
    .get(requestedSessionId);
  if (!row) {
    throw new Error(`No messages found for session: ${requestedSessionId}`);
  }
  sessionId = requestedSessionId;
} else {
  const row = db
    .prepare(
      "SELECT session_id, COUNT(*) AS n, MAX(time_created) AS m " +
        "FROM message GROUP BY session_id ORDER BY m DESC LIMIT 1",
    )
    .get();
  sessionId = row.session_id;
  console.log(`Selected most recent session: ${sessionId} (${row.n} messages)`);
}

const messages = db
  .prepare("SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created")
  .all(sessionId);
const partStmt = db.prepare(
  "SELECT data FROM part WHERE message_id = ? ORDER BY time_created",
);

const lines = [];
for (const msg of messages) {
  const message = JSON.parse(msg.data);
  const role = message.role === "user" ? "user" : "assistant";
  const content = [];
  for (const partRow of partStmt.all(msg.id)) {
    const part = JSON.parse(partRow.data);
    if (part.type === "text" && part.text?.trim()) {
      content.push({ type: "text", text: part.text });
    } else if (part.type === "tool" && part.tool) {
      content.push({
        type: "tool_use",
        id: part.callID ?? `${msg.id}-${content.length}`,
        name: part.tool,
        input: part.state?.input ?? {},
      });
    } else if (part.type === "reasoning" && part.text?.trim()) {
      content.push({ type: "thinking", thinking: part.text });
    }
  }
  if (content.length === 0) continue;
  lines.push(JSON.stringify({ type: role, message: { role, content } }));
}

if (lines.length === 0) {
  throw new Error(`Session ${sessionId} produced no convertible content blocks`);
}

const projectDirName = process.cwd().replace(/[^A-Za-z0-9]/g, "-");
const claudeProjectsDir = join(homedir(), ".claude", "projects", projectDirName);
mkdirSync(claudeProjectsDir, { recursive: true });
const jsonlPath = join(claudeProjectsDir, `${sessionId}.jsonl`);
writeFileSync(jsonlPath, lines.join("\n") + "\n", "utf8");

console.log(`Wrote ${lines.length} transcript entries for ${sessionId}`);
console.log(jsonlPath);
console.log(`Next: cleo transcript extract ${sessionId} --dry-run`);
