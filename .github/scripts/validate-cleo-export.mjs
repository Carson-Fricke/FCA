import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const exportPath = resolve(process.cwd(), ".cleo-export.json");
const data = JSON.parse(readFileSync(exportPath, "utf8"));
const tasks = data.tasks;

if (data?._meta?.format !== "cleo-export" || !Array.isArray(tasks)) {
  throw new Error("Invalid CLEO export package");
}

if (data._meta.taskCount !== tasks.length) {
  throw new Error(`Task count mismatch: metadata=${data._meta.taskCount}, tasks=${tasks.length}`);
}

const byId = new Map();
for (const task of tasks) {
  if (!/^T\d+$/.test(task.id) || byId.has(task.id)) {
    throw new Error(`Invalid or duplicate task ID: ${task.id}`);
  }
  byId.set(task.id, task);
}

const parentTypes = {
  epic: new Set(["saga"]),
  task: new Set(["epic"]),
  subtask: new Set(["task"]),
};

for (const task of tasks) {
  if (task.parentId) {
    const parent = byId.get(task.parentId);
    if (!parent) {
      throw new Error(`Missing parent ${task.parentId} for ${task.id}`);
    }
    if (!parentTypes[task.type]?.has(parent.type)) {
      throw new Error(`Invalid hierarchy: ${parent.type} ${parent.id} -> ${task.type} ${task.id}`);
    }
  } else if (!new Set(["saga", "epic"]).has(task.type)) {
    throw new Error(`Invalid root task type for ${task.id}: ${task.type}`);
  }

  const summary = data.idMap?.[task.id];
  if (!summary || summary.title !== task.title || summary.status !== task.status) {
    throw new Error(`Missing or stale idMap entry for ${task.id}`);
  }

  for (const dependencyId of task.depends ?? []) {
    if (dependencyId === task.id || !byId.has(dependencyId)) {
      throw new Error(`Invalid dependency ${task.id} -> ${dependencyId}`);
    }
  }
}

const visiting = new Set();
const visited = new Set();
function visit(taskId) {
  if (visiting.has(taskId)) {
    throw new Error(`Dependency cycle at ${taskId}`);
  }
  if (visited.has(taskId)) return;

  visiting.add(taskId);
  for (const dependencyId of byId.get(taskId).depends ?? []) visit(dependencyId);
  visiting.delete(taskId);
  visited.add(taskId);
}

for (const taskId of byId.keys()) visit(taskId);

const maxId = Math.max(...byId.keys().map((id) => Number(id.slice(1))));
if (data._meta.source?.nextId !== maxId + 1) {
  throw new Error(`Invalid nextId: expected ${maxId + 1}, got ${data._meta.source?.nextId}`);
}

console.log(`Validated ${tasks.length} CLEO tasks through T${maxId}.`);
