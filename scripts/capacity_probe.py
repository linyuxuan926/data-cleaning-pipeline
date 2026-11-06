"""Measure one bounded real workload; a measurement, not a capacity promise."""
from __future__ import annotations
import argparse
import csv
import io
import json
import math
from pathlib import Path
import statistics
import subprocess
import time
import uuid
from smoke_test import Client

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8088")
    parser.add_argument("--project", default="data-cleaning-pipeline")
    args = parser.parse_args()
    def docker(*parts):
        value = subprocess.run(["docker", "compose", "-p", args.project, *parts], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        assert value.returncode == 0, value.stderr[-1000:]
        return value.stdout
    n, distinct = 50001, 5000
    source = io.StringIO(newline="")
    writer = csv.writer(source, lineterminator="\n")
    writer.writerow(["id", "amount", "label"])
    raw = []
    for index in range(n):
        amount = None if index % 11 == 0 else 10000 if index == 49999 else index % 100
        raw.append(amount)
        writer.writerow([f"{index%distinct:06d}", "" if amount is None else amount, "  Mixed Case  "])
    data = source.getvalue().encode()
    valid = [value for value in raw if value is not None]
    expected_mean = math.fsum(valid) / len(valid)
    filled = [expected_mean if value is None else value for value in raw]
    expected_median = statistics.median(filled)
    auth = Client(args.base_url).request("POST", "/api/auth/register", {"username": "capacity_"+uuid.uuid4().hex[:10], "password": uuid.uuid4().hex})
    owner = Client(args.base_url, auth["token"])
    file_id = owner.upload("global-statistics.csv", data)
    rules = [
        {"type": "trim", "columns": ["label"]},
        {"type": "cast", "columns": ["amount"], "dtype": "number"},
        {"type": "fill_stat", "columns": ["amount"], "strategy": "mean", "emptyPolicy": "fail"},
        {"type": "fill_stat", "columns": ["amount"], "strategy": "median", "emptyPolicy": "fail"},
        {"type": "iqr", "column": "amount", "factor": 1.5, "emptyPolicy": "fail", "onError": "mark"},
        {"type": "deduplicate", "columns": ["id"], "keep": "last"},
    ]
    rule = owner.request("POST", "/api/rule-sets", {"name": "Measured global statistics", "rules": rules})
    # Dedicated fresh project keeps the process high-water mark interpretable.
    docker("restart", "worker")
    started = time.monotonic()
    task = owner.request("POST", "/api/tasks", {"fileId": file_id, "ruleSetId": rule["id"], "ruleVersion": 1, "outputFormat": "csv"})
    task = owner.wait(task["id"], 240)
    elapsed = time.monotonic()-started
    assert task["status"] == "SUCCEEDED", task
    report = owner.request("GET", f"/api/tasks/{task['id']}/report")
    result = list(csv.DictReader(io.StringIO(owner.request("GET", f"/api/tasks/{task['id']}/artifacts/result", raw=True).decode("utf-8-sig"))))
    assert len(result) == distinct and report["finalRemovedRows"] == n-distinct
    assert report["duplicateRemovedRows"] == n-distinct and report["outlierHandledRows"] == 1
    assert report["validOutputRows"] == distinct-1 and report["flaggedOutputRows"] == 1
    assert report["missingFilledCells"] == raw.count(None)
    for index, row in zip(range(n-distinct,n), result):
        assert row["id"] == f"{index%distinct:06d}" and row["label"] == "Mixed Case", (index,row)
        assert math.isclose(float(row["amount"]), filled[index], rel_tol=1e-12)
    assert math.isclose(float(report["rules"][2]["statistics"]["f1"]["fillValue"]), expected_mean, rel_tol=1e-12)
    assert math.isclose(float(report["rules"][3]["statistics"]["f1"]["fillValue"]), expected_median, rel_tol=1e-12)
    ordered = sorted(filled)
    def quantile(q):
        position = (n-1)*q
        lower = math.floor(position)
        upper = math.ceil(position)
        return ordered[lower]+(ordered[upper]-ordered[lower])*(position-lower)
    assert math.isclose(report["rules"][4]["statistics"]["q1"], quantile(.25), rel_tol=1e-12)
    assert math.isclose(report["rules"][4]["statistics"]["q3"], quantile(.75), rel_tol=1e-12)
    status = docker("exec", "-T", "worker", "python", "-c", "from pathlib import Path; print(Path('/proc/1/status').read_text())")
    peak_kib = int(next(line.split()[1] for line in status.splitlines() if line.startswith("VmHWM:")))
    runtime = subprocess.run(["docker", "info", "--format", "{{json .}}"], capture_output=True, text=True, encoding="utf-8", timeout=30)
    machine = json.loads(runtime.stdout)
    measured = {"taskId": task["id"], "project": args.project, "dockerLogicalCpus": machine["NCPU"], "dockerMemoryBytes": machine["MemTotal"], "format": "csv", "bytes": len(data), "inputRows": n, "columns": 3, "concurrentTasks": 1, "rules": rules, "wallSecondsIncludingQueue": round(elapsed,3), "engineSeconds": report["durationSeconds"], "workerPeakRssKiB": peak_kib, "expectedMean": expected_mean, "expectedMedian": expected_median, "outputRows": len(result), "allOutputRowsChecked": True, "executionModel": "bounded full DataFrame; no streaming claim"}
    target = ROOT / ".test-output/v3-capacity.json"
    target.write_text(json.dumps(measured, ensure_ascii=False, indent=2),encoding="utf-8")
    print("PASS: all 5000 output records, global fill, IQR, cross-file keep-last and report conservation checked.")
    print(json.dumps(measured, ensure_ascii=False))


if __name__ == "__main__":
    main()
