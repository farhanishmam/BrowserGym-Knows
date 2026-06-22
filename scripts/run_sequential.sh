#!/usr/bin/env bash
# Sequential live benchmark queue with restart-resume + inline env support.
#
# Queue file: run_sequential.queue
#   Each non-empty, non-comment line is one job. Format:
#     [KEY=VAL ...] <script.py> <benchmark> [n_jobs]
#   Examples:
#     gpt55_axt_screenshot.py knows_sheets_38
#     gpt55_axt_screenshot.py knows_sheets_38 1
#     KNOWS_TASKS=knows.sheets_38_apartment_finder.1 gpt55_axt_screenshot.py knows_sheets_38
#
# Append jobs at any time (even while the runner is mid-job):
#   echo "gpt55_axt_screenshot.py knows_sheets_45" >> run_sequential.queue
#
# Restart-resume:
#   run_sequential.checkpoint stores how many queued jobs have already
#   completed in this queue file. On startup the script skips that many
#   non-comment lines. To force a re-run of the entire queue:
#     rm run_sequential.checkpoint
#
# Stop after the queue drains:
#   touch run_sequential.stop
#   (an in-flight job is never interrupted; Ctrl-C still works too)
#
# Status / dry-run:
#   ./run_sequential.sh --status
#
# Logs land in logs/run_sequential/<timestamp>/:
#   - run.log captures the queue-level status.
#   - one per-job log captures each benchmark's stdout/stderr.

set -uo pipefail

# This script lives in scripts/; the repo root is its parent directory.
cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

QUEUE_FILE="${SEQUENTIAL_QUEUE_FILE:-$REPO_ROOT/run_sequential.queue}"
CHECKPOINT_FILE="${SEQUENTIAL_CHECKPOINT_FILE:-$REPO_ROOT/run_sequential.checkpoint}"
STOP_FILE="${SEQUENTIAL_STOP_FILE:-$REPO_ROOT/run_sequential.stop}"
POLL_SECONDS="${SEQUENTIAL_POLL_SECONDS:-10}"

# Keep both the queue and each benchmark study strictly sequential.
export BROWSERGYM_N_JOBS=1

# Run the benchmark evaluator at task completion/finalize time for every job.
export KNOWS_RUN_EVALUATORS=1

# Create the queue file with just a header if missing. No auto-seeded jobs.
if [[ ! -f "$QUEUE_FILE" ]]; then
    cat >"$QUEUE_FILE" <<'EOF'
# Live queue for run_sequential.sh.
# Each non-empty, non-comment line is one job:
#   [KEY=VAL ...] <script.py> <benchmark> [n_jobs]
# Append while the runner is active, e.g.:
#   echo "gpt55_axt_screenshot.py knows_sheets_45" >> run_sequential.queue
EOF
fi

# Count non-comment, non-blank queue lines.
count_queue_jobs() {
    local total=0 raw line
    while IFS= read -r raw || [[ -n "$raw" ]]; do
        line="${raw%%#*}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -n "$line" ]] || continue
        total=$((total + 1))
    done <"$QUEUE_FILE"
    printf '%d' "$total"
}

# Echo the Nth (1-indexed) non-comment, non-blank line of the queue.
read_job_at() {
    local target="$1"
    local seen=0 raw line
    while IFS= read -r raw || [[ -n "$raw" ]]; do
        line="${raw%%#*}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -n "$line" ]] || continue
        seen=$((seen + 1))
        if [[ "$seen" -eq "$target" ]]; then
            printf '%s\n' "$line"
            return 0
        fi
    done <"$QUEUE_FILE"
    return 1
}

# Parse one queue line into JOB_ENV (array), JOB_SCRIPT, JOB_BENCHMARK, JOB_N_JOBS.
# Leading tokens shaped like KEY=VAL become env vars; the first non-KEY=VAL token
# is the script, the next is the benchmark, and an optional final token is n_jobs.
parse_job_line() {
    local line="$1"
    JOB_ENV=()
    JOB_SCRIPT=""
    JOB_BENCHMARK=""
    JOB_N_JOBS="$BROWSERGYM_N_JOBS"

    local tokens=()
    read -ra tokens <<<"$line"
    [[ ${#tokens[@]} -gt 0 ]] || return 0

    local positional=()
    local seen_positional=0 tok
    for tok in "${tokens[@]}"; do
        [[ -z "$tok" ]] && continue
        if [[ "$seen_positional" -eq 0 && "$tok" == *=* && "$tok" != =* ]]; then
            JOB_ENV+=("$tok")
        else
            seen_positional=1
            positional+=("$tok")
        fi
    done

    [[ ${#positional[@]} -ge 1 ]] && JOB_SCRIPT="${positional[0]}"
    [[ ${#positional[@]} -ge 2 ]] && JOB_BENCHMARK="${positional[1]}"
    [[ ${#positional[@]} -ge 3 ]] && JOB_N_JOBS="${positional[2]}"
}

load_checkpoint() {
    local raw_cp="0"
    if [[ -f "$CHECKPOINT_FILE" ]]; then
        raw_cp="$(tr -d '[:space:]' <"$CHECKPOINT_FILE" 2>/dev/null || echo 0)"
    fi
    [[ "$raw_cp" =~ ^[0-9]+$ ]] || raw_cp=0
    printf '%d' "$raw_cp"
}

save_checkpoint() {
    local value="$1"
    local tmp
    tmp="$(mktemp "${CHECKPOINT_FILE}.XXXXXX")"
    printf '%s\n' "$value" >"$tmp"
    mv -f "$tmp" "$CHECKPOINT_FILE"
}

# --status: print queue / checkpoint summary and exit.
if [[ "${1:-}" == "--status" ]]; then
    done_count="$(load_checkpoint)"
    total="$(count_queue_jobs)"
    pending=$(( total - done_count ))
    if [[ "$pending" -lt 0 ]]; then pending=0; fi

    echo "queue=$QUEUE_FILE"
    echo "checkpoint=$CHECKPOINT_FILE (done=$done_count)"
    echo "total_queue_jobs=$total  pending=$pending"
    if [[ "$pending" -gt 0 ]]; then
        echo "Pending jobs:"
        i="$done_count"
        while :; do
            i=$((i + 1))
            [[ "$i" -gt "$total" ]] && break
            if line="$(read_job_at "$i")"; then
                printf '  %02d  %s\n' "$i" "$line"
            else
                break
            fi
        done
    fi
    exit 0
fi

TS="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
LOG_DIR="$REPO_ROOT/logs/run_sequential/$TS"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/run.log"

# Clear any stale stop request from a previous invocation.
rm -f "$STOP_FILE"

print_banner() {
    local msg="$1"
    {
        echo
        echo "============================================================"
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $msg"
        echo "============================================================"
    } | tee -a "$RUN_LOG"
}

processed="$(load_checkpoint)"
failed=()
idle_notice_printed=0

print_banner "Starting live sequential run (n_jobs=${BROWSERGYM_N_JOBS}, queue=$QUEUE_FILE, log_dir=$LOG_DIR, resume_after=$processed)"

while true; do
    next=$((processed + 1))
    if ! job_line="$(read_job_at "$next")"; then
        if [[ -f "$STOP_FILE" ]]; then
            print_banner "Stop file detected and no queued jobs remain"
            break
        fi
        if [[ "$idle_notice_printed" -eq 0 ]]; then
            print_banner "Queue empty after $processed job(s); waiting for appended jobs (poll=${POLL_SECONDS}s)"
            idle_notice_printed=1
        fi
        sleep "$POLL_SECONDS"
        continue
    fi
    idle_notice_printed=0

    parse_job_line "$job_line"
    if [[ -z "$JOB_SCRIPT" || -z "$JOB_BENCHMARK" ]]; then
        printf '%s [SKIP] malformed queue line %d: %s\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$next" "$job_line" \
            | tee -a "$RUN_LOG"
        processed="$next"
        save_checkpoint "$processed"
        continue
    fi

    job_slug="$(printf '%02d_%s_%s' "$next" "${JOB_SCRIPT%.py}" "$JOB_BENCHMARK")"
    job_log="$LOG_DIR/${job_slug}.log"

    env_preview=""
    if [[ ${#JOB_ENV[@]} -gt 0 ]]; then
        env_preview=" env=[${JOB_ENV[*]}]"
    fi
    print_banner "[$next]${env_preview} script=$JOB_SCRIPT benchmark=$JOB_BENCHMARK n_jobs=$JOB_N_JOBS log=$job_log"

    if [[ ${#JOB_ENV[@]} -gt 0 ]]; then
        cmd=(env "${JOB_ENV[@]}" "$REPO_ROOT/scripts/run_one.sh" "$JOB_SCRIPT" "$JOB_BENCHMARK" "$JOB_N_JOBS")
    else
        cmd=("$REPO_ROOT/scripts/run_one.sh" "$JOB_SCRIPT" "$JOB_BENCHMARK" "$JOB_N_JOBS")
    fi

    start_ts="$(date -u +%s)"
    if { time "${cmd[@]}"; } >"$job_log" 2>&1; then
        elapsed=$(( $(date -u +%s) - start_ts ))
        printf '%s [OK]  job=%s elapsed=%ds\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$job_slug" "$elapsed" \
            | tee -a "$RUN_LOG"
    else
        rc=$?
        elapsed=$(( $(date -u +%s) - start_ts ))
        printf '%s [FAIL] job=%s rc=%d elapsed=%ds (continuing)\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$job_slug" "$rc" "$elapsed" \
            | tee -a "$RUN_LOG"
        failed+=("$job_slug (rc=$rc)")
    fi
    processed="$next"
    save_checkpoint "$processed"
done

print_banner "Sequential run finished"
if [[ ${#failed[@]} -gt 0 ]]; then
    {
        echo "Failed jobs (${#failed[@]} of $processed processed):"
        for f in "${failed[@]}"; do
            echo "  - $f"
        done
    } | tee -a "$RUN_LOG"
    exit 1
fi
echo "All $processed processed job(s) completed successfully." | tee -a "$RUN_LOG"
