#!/usr/bin/env bash
# 腳本測試台：跑一輪（或多輪）全套劇本，無人值守。
#
# 設計見 docs/specs/2026-09-05-script-harness-design.md。
# 每個劇本是一場獨立會議（狀態不互相污染，見該文件「決定四」），一律 --mute
# （不需要音訊裝置、不燒 TTS 額度、時序行為與有聲完全相同）。
#
#   scripts/run-script-suite.sh                     # 全部劇本跑一輪
#   scripts/run-script-suite.sh imbalance healthy   # 只跑指定的
#   AHEM_SUITE_PARALLEL=3 scripts/run-script-suite.sh
#
# 平行度預設 1（序列）。獨立場次本來就可以平行，但每個行程每 5 秒打一次判斷
# LLM，平行度開太高會撞到速率限制——不確定就從 3 開始。
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON_BIN="${AHEM_PYTHON:-.venv/bin/python}"
PARALLEL="${AHEM_SUITE_PARALLEL:-1}"
OUT="${AHEM_SUITE_OUT:-$(mktemp -d)/suite-$(date +%m%d-%H%M)}"
mkdir -p "$OUT"

if [ $# -gt 0 ]; then
  SCRIPTS=()
  for n in "$@"; do SCRIPTS+=("examples/scripts/$n.json"); done
else
  SCRIPTS=(examples/scripts/*.json)
fi

echo "[suite] ${#SCRIPTS[@]} 個劇本，平行度 ${PARALLEL}，輸出 $OUT"

run_one () {
  local path="$1" name
  name="$(basename "$path" .json)"
  PYTHONPATH=src "$PYTHON_BIN" -u -m meeting_host.live \
    --script "$path" --mute --say-hello \
    > "$OUT/$name.log" 2>&1 < /dev/null || true
  # 劇本播完會自己收尾（live.end_after_script），所以這裡不需要逾時或 kill
  printf "[suite] %-17s 開口 %s 次\n" "$name" "$(grep -c '🗣' "$OUT/$name.log" || echo 0)"
}

i=0
for path in "${SCRIPTS[@]}"; do
  run_one "$path" &
  i=$((i + 1))
  if [ "$((i % PARALLEL))" -eq 0 ]; then wait; fi
done
wait

echo
echo "[suite] ── 摘要 ──"
for path in "${SCRIPTS[@]}"; do
  name="$(basename "$path" .json)"
  log="$OUT/$name.log"
  [ -f "$log" ] || continue
  echo "  ▸ $name"
  grep -E '🗣' "$log" | sed 's/^ */      /' || echo "      （全程沒有開口）"
done
echo
echo "[suite] 完整輸出在 $OUT"
