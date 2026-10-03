import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

const source = readFileSync(new URL("../src/api/applicationPolicy.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS },
}).outputText;

function loadPolicy(fetcher) {
  const exports = {};
  vm.runInNewContext(compiled, {
    exports,
    require: () => ({ fetcher }),
  });
  return exports.fetchApplicationPolicy;
}

for (const days of [1, 2, 3, 5, 7]) {
  test(`accepts a ${days}-day GPU limit and independent CPU limit`, async () => {
    const fetchPolicy = loadPolicy(async (path) => {
      assert.equal(path, "/containers/application-policy");
      return { gpu_max_lease_days: days, cpu_max_lease_days: 7 };
    });
    assert.deepEqual(JSON.parse(JSON.stringify(await fetchPolicy())), {
      gpu_max_lease_days: days, cpu_max_lease_days: 7,
    });
  });
}

for (const invalid of [null, {}, { gpu_max_lease_days: 0, cpu_max_lease_days: 7 },
  { gpu_max_lease_days: "3", cpu_max_lease_days: 7 },
  { gpu_max_lease_days: 1.5, cpu_max_lease_days: 7 },
  { gpu_max_lease_days: 3, cpu_max_lease_days: -1 }]) {
  test(`rejects invalid policy ${JSON.stringify(invalid)} without a fallback`, async () => {
    await assert.rejects(loadPolicy(async () => invalid)(), {
      message: "使用期限加载失败，请重试",
    });
  });
}

test("does not pass through server error details", async () => {
  const fetchPolicy = loadPolicy(async () => { throw new Error("private server detail"); });
  await assert.rejects(fetchPolicy(), { message: "使用期限加载失败，请重试" });
});

test("whitelists only public lease-limit fields", async () => {
  const fetchPolicy = loadPolicy(async () => ({
    gpu_max_lease_days: 3, cpu_max_lease_days: 7, private_detail: "not for display",
  }));
  assert.deepEqual(Object.keys(await fetchPolicy()).sort(), ["cpu_max_lease_days", "gpu_max_lease_days"]);
});
