#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

python_bin="${AHEM_PYTHON:-$repo_dir/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  echo "找不到 Python 環境：$python_bin" >&2
  echo "請先執行 scripts/install-secure.sh，或設定 AHEM_PYTHON。" >&2
  exit 2
fi

run_dir="$repo_dir/.security-run"
mkdir -p "$run_dir"

pytest_output="$run_dir/pytest.txt"
"$python_bin" -m pytest -q | tee "$pytest_output"
"$python_bin" -m pip check
"$python_bin" -m pip_audit --local
"$python_bin" -m pip_audit --local --format cyclonedx-json --output sbom.cdx.json
"$python_bin" -m bandit -q -lll -r src

summary="$(tail -n 1 "$pytest_output")"
passed="$(printf '%s' "$summary" | grep -Eo '[0-9]+ passed' | awk '{print $1}' || true)"
skipped="$(printf '%s' "$summary" | grep -Eo '[0-9]+ skipped' | awk '{print $1}' || true)"
xfailed="$(printf '%s' "$summary" | grep -Eo '[0-9]+ xfailed' | awk '{print $1}' || true)"

if [[ -z "$passed" ]]; then
  echo "無法解析 pytest 摘要：$summary" >&2
  exit 3
fi

report="$run_dir/summary.json"
"$python_bin" -c 'import json,sys; json.dump({"pytest":{"passed":int(sys.argv[1]),"skipped":int(sys.argv[2] or 0),"xfailed":int(sys.argv[3] or 0)},"pip_check":"No broken requirements found","pip_audit":"No known vulnerabilities found","bandit":"0 個 High severity finding"},open(sys.argv[4],"w"),ensure_ascii=False,indent=2)' "$passed" "$skipped" "$xfailed" "$report"

"$python_bin" scripts/update_project_status.py --report "$report"
echo "已更新 PROJECT_STATUS.md（僅含非敏感摘要）。"
