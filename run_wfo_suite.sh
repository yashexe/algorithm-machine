#!/usr/bin/env bash
# run_wfo_suite.sh — full WFO parameter sweep for equity_pullback_swing
#
# Usage:
#   chmod +x run_wfo_suite.sh
#   ./run_wfo_suite.sh
#
# Each run's console output is saved to logs/wfo_suite/<name>.log
# WFO artefacts (equity curves, fold summaries) go to results/<name>/
# Runtime per run: 3–15 min (warm cache, 4 workers)
# Total suite:     ~60–90 min
#
# To run the equity_momentum suite instead:
#   CFG=config/equity_momentum.yaml ./run_wfo_suite.sh

set -euo pipefail

SUITE_START=$(date +%s)
LOG_DIR="logs/wfo_suite"
PY=".venv/bin/python"
CFG="${CFG:-config/equity_pullback_swing.yaml}"
W="--workers 4"

# ── 1. Clear old results and logs ───────────────────────────────────────────
echo "==> Clearing results/ and logs/ ..."
rm -rf results/* logs/*
mkdir -p "$LOG_DIR"
echo "    Done."
echo

# ── Helper: run one WFO config, tee output to log ───────────────────────────
run() {
    local name="$1"; shift
    local log="$LOG_DIR/${name}.log"
    echo "════════════════════════════════════════════════════════════════"
    echo "  START: $name  ($(date '+%H:%M:%S'))"
    echo "  CMD:   $PY run_walk_forward.py $W $*"
    echo "════════════════════════════════════════════════════════════════"
    local t0=$(date +%s)
    $PY run_walk_forward.py $W --output-dir "results/${name}" "$@" 2>&1 | tee "$log"
    local elapsed=$(( $(date +%s) - t0 ))
    echo
    echo "  DONE: $name  — ${elapsed}s  (log: $log)"
    echo
}

# ════════════════════════════════════════════════════════════════════════════
# Sweep 1 — Window sizes
# Question: does the edge survive different IS/OOS granularities?
# ════════════════════════════════════════════════════════════════════════════

run "w_is2_oos1" --config "$CFG" \
    --is-years 2 --oos-years 1
# 7 folds, shorter IS → more folds, better statistics

run "w_is4_oos1" --config "$CFG" \
    --is-years 4 --oos-years 1
# 5 folds, longer IS → deeper training windows

run "w_is3_oos2" --config "$CFG" \
    --is-years 3 --oos-years 2
# 3 folds, 2-yr OOS → tests if 2020 outlier stops carrying the aggregate

run "w_is2_oos2" --config "$CFG" \
    --is-years 2 --oos-years 2
# 3 folds, short IS + long OOS → hardest test of param stability

# ════════════════════════════════════════════════════════════════════════════
# Sweep 2 — Optimization metric
# Question: does the IS objective matter for OOS quality?
# All use IS=3yr OOS=1yr to isolate the metric change.
# ════════════════════════════════════════════════════════════════════════════

run "m_sortino" --config "$CFG" \
    --metric sortino_ratio
# Penalises downside harder — may select params that protect bear markets better

run "m_return" --config "$CFG" \
    --metric annualized_return_pct
# Pure return maximiser — tests if Sharpe is suppressing high-return params

run "m_calmar" --config "$CFG" \
    --metric calmar_ratio
# Return per unit of max drawdown — most conservative selection criterion

# ════════════════════════════════════════════════════════════════════════════
# Sweep 3 — Wider parameter ranges (targeted hypotheses)
# Grid overrides use --grid JSON; keys must be valid strategy params.
# ════════════════════════════════════════════════════════════════════════════

run "p_entry_rsi" --config "$CFG" \
    --grid '{"entry_rsi":[20,25,30,35,40,45],"exit_rsi":[55,60,65],"max_hold_bars":[20,30],"trail_ma_period":[15,20],"trail_activation_pct":[0.03,0.05],"position_pct":[0.08,0.10,0.12]}'
# Hypothesis: wider entry_rsi range finds whether earlier entries (RSI 20-25) improve OOS

run "p_hold" --config "$CFG" \
    --grid '{"entry_rsi":[25,30,35],"exit_rsi":[55,60,65],"max_hold_bars":[10,20,30,45,60],"trail_ma_period":[15,20],"trail_activation_pct":[0.03,0.05],"position_pct":[0.08,0.10,0.12]}'
# Hypothesis: max_hold_bars is the dominant exit lever — scan 10-60 bar range

run "p_sizing" --config "$CFG" \
    --grid '{"entry_rsi":[25,30,35],"exit_rsi":[55,60,65],"max_hold_bars":[20,30],"trail_ma_period":[15,20],"trail_activation_pct":[0.03,0.05],"position_pct":[0.06,0.08,0.10,0.12,0.15]}'
# Hypothesis: position sizing (concentration) has strong impact on risk-adjusted return

# ════════════════════════════════════════════════════════════════════════════
# Sweep 4 — Combined wide grid (uses wfo_grid from config as-is)
# 3×3×2×2×2×3 = 216 combos/fold. Full exhaustive search of config grid.
# Question: does any corner of the parameter space show consistent OOS edge?
# ════════════════════════════════════════════════════════════════════════════

run "wide" --config "$CFG"
# No --grid: uses the wfo_grid defined in equity_pullback_swing.yaml directly

# ════════════════════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════════════════════

SUITE_ELAPSED=$(( $(date +%s) - SUITE_START ))
SUITE_MIN=$(( SUITE_ELAPSED / 60 ))
SUITE_SEC=$(( SUITE_ELAPSED % 60 ))

echo "════════════════════════════════════════════════════════════════"
echo "  SUITE COMPLETE  —  total time: ${SUITE_MIN}m ${SUITE_SEC}s"
echo "════════════════════════════════════════════════════════════════"
echo
echo "Results:   results/<name>/wfo_summary.json"
echo "CLI logs:  logs/wfo_suite/<name>.log"
echo
echo "Quick aggregate comparison:"
printf "  %-18s  %12s  %12s  %10s  %8s\n" "Run" "OOS Ann Ret" "OOS Sharpe" "OOS MaxDD" "Folds"
for d in results/*/; do
    name=$(basename "$d")
    json="$d/wfo_summary.json"
    if [[ -f "$json" ]]; then
        python3 -c "
import json
with open('$json') as f:
    s = json.load(f)
ret   = s.get('aggregate_oos_ann_return', float('nan'))
shr   = s.get('aggregate_oos_sharpe',     float('nan'))
dd    = s.get('aggregate_oos_max_dd',     float('nan'))
folds = s.get('num_folds', '?')
print(f'  {\"$name\":<18}  {ret:+.2%}        {shr:+.2f}      {dd:.2%}    {folds}')
" 2>/dev/null || echo "  $name  (parse error)"
    fi
done
echo
