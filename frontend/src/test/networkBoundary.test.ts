import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

// Guards master-prompt rule: "The frontend must not directly connect
// to MT5" / "no direct frontend -> MT5 connection". This walks every
// source file and fails the suite if any forbidden term or a
// non-/api network target ever creeps in.
const SRC_DIR = join(__dirname, "..");
const FORBIDDEN_TERMS = ["MetaTrader5", "mt5://", "MT5_LOGIN", "MT5_PASSWORD", "broker_password", "broker_login"];

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    if (entry === "test" || entry.startsWith(".")) continue;
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) out.push(...walk(full));
    else if (/\.(ts|tsx)$/.test(entry)) out.push(full);
  }
  return out;
}

describe("frontend network boundary", () => {
  const files = walk(SRC_DIR);

  it("scans at least the expected source files", () => {
    expect(files.length).toBeGreaterThan (5);
  });

  it("never references MT5/broker credentials or direct MT5 connection strings", () => {
    for (const file of files) {
      const content = readFileSync(file, "utf-8");
      for (const term of FORBIDDEN_TERMS) {
        expect(content, `${file} must not reference "${term}"`).not.toContain(term);
      }
    }
  });

  it("api.ts only ever calls the backend's own /api/* routes", () => {
    const apiSource = readFileSync(join(SRC_DIR, "lib", "api.ts"), "utf-8");
    const pathLiterals = [...apiSource.matchAll(/["'`](\/[a-zA-Z0-9/_-]*)["'`]/g)].map((m) => m[1]);
    for (const p of pathLiterals) {
      expect(p.startsWith("/api/"), `unexpected non-/api path literal: ${p}`).toBe(true);
    }
  });
});
