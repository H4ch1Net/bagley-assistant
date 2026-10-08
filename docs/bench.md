# Model benchmark and machines

`bagley bench` tests every model installed on your machines on real tool-use tasks and tells you
which one to use on each. `bagley machines` shows the machines Bagley routes to and adds or
removes them.

## What the benchmark does

Each task is a request a user would make, a set of stub tools that return canned data, and checks
on what the model does. The stub tools have the same schemas and argument checks as Bagley's real
tools, but nothing real happens: no web, no files, no email. Every model gets the same nine tools
for every task, so picking the right one is part of the test.

| Task | What it checks |
|---|---|
| `weather_lisbon` | Calls `get_weather` for Lisbon (not `web_search`) and reports 22 °C |
| `tip_math` | Uses `calculate` for 17.5 % of 2,340 and answers 409.5 |
| `time_tokyo` | Calls `get_current_time` with Tokyo's time zone and reads the time back |
| `find_notes` | Searches the files for the Lisbon trip notes and names the file |
| `marathon_km` | Converts 26.2 miles to km with `convert_units` (or `calculate`): 42.16 |
| `lisbon_fahrenheit` | Two steps: the weather, then the temperature in Fahrenheit (71.6) |
| `capital_no_tool` | Answers "Paris" **without** calling any tool |
| `calendar_pick` | Picks `create_calendar_event` among distractors, with the right date and time |
| `messy_email` | Pulls the address, subject and message out of a sloppy request |
| `compare_cities` | Looks up two cities and compares them |

Argument checks are tolerant: `Lisboa, PT` counts as Lisbon, `0.175 * 2,340` and `2340*17.5/100`
are both right, `miles`, `mi` and `Mile` are the same unit. A wrong call followed by a corrected
one still passes. Each task gets at most 4 model calls; a reply has 180 s.

`bagley bench --list-tasks` prints the prompts.

**How it runs.** Models are tested one at a time. Each is loaded first with a tiny request, so
loading time doesn't count against the first task. Models with native tool calling get the tools
natively; the others (and any model whose server refuses tools) get the text protocol Bagley uses
in chat (prompt mode). Temperature is 0.2; the context size and the thinking switch come from your
settings. Embedding models are skipped. On a `cloud` machine only its configured model is tested,
unless you name models with `--models` (hosted APIs list dozens of paid models).

**Scoring.** Score = 100 × pass rate − 1 point per second of average task time (at most 10). The
winner on each machine is the highest score; ties go to the faster model, then to native tool
calling. A model that passed nothing never wins.

Measured per task: pass or fail with the reason, wall time, time to the first token, and tokens
per second when the server reports it (Ollama does).

## Running it

```sh
bagley bench                              # every machine, every chat model
bagley bench --machine h4ch1              # one machine (repeat --machine for more)
bagley bench --models 'qwen3*' gpt-oss:20b
bagley bench --tasks weather_lisbon messy_email
bagley bench --apply                      # make the winners the default models
bagley bench --json                       # the full result as JSON
```

```
BENCH 3F2A9C1B // 10 TASKS // 50 RUNS
[01/50] H4CH1 gpt-oss:20b weather_lisbon ...................... OK 2.1s
[02/50] H4CH1 gpt-oss:20b tip_math ............................ OK 1.4s
...
[23/50] H4CH1 qwen2.5:32b calendar_pick ....................... FAIL 6.8s  called web_search, which this task does not need

H4CH1 // GPU
   MODEL                 PASS     AVG   TOK/S   SCORE
>  qwen3:14b            10/10    1.8s    48.2    98.2
   gpt-oss:20b          10/10    2.3s    61.0    97.7
   qwen2.5:32b          08/10    6.9s    11.4    73.1
B1T // LOCAL
   MODEL                 PASS     AVG   TOK/S   SCORE
>  qwen3:4b             08/10    3.2s    21.7    76.8
   llama3.2:3b          06/10    1.1s    35.0    58.9
[OK] APPLIED  H4CH1 -> qwen3:14b
[OK] APPLIED  LOCAL -> qwen3:4b
```

`--apply` sets this machine's model (`model` in the preferences) and each other machine's
`model`. A model fixed by `BAGLEY_MODEL` is left alone and reported. Results are kept in the
database (`bench_results`), and the latest run per machine is available to the web UI.

The command uses the running server when there is one, so the run shows up there too; otherwise
it runs in the terminal.

### API

- `GET /api/bench` returns `{"running": bool, "tasks": [{id, title, prompt}], "machines": [...]}`
  with, for each machine, its latest run: `{machine, name, run_id, created_at, winner,
  models: [{model, mode, passed, total, avg_seconds, ttft, tokens_per_second, score, winner,
  error, load_seconds, tasks: [...]}]}`.
- `POST /api/bench/run` with `{"machines": [], "models": [], "tasks": [], "apply": false}` (all
  optional) streams NDJSON: `bench.start`, a `bench.task` per task, a `bench.model` per model, then
  `bench.done` with the result and what `apply` changed, or `error`. One run at a time (`409`
  otherwise). The run stops if the client goes away, unless `"detach": true`.

## Machines

Bagley routes each request to a machine: with routing on `auto`, heavy work goes to the first GPU
machine that answers, then this machine, then other local machines, then cloud fallbacks. Light
work (chat titles) stays on this machine.

```sh
bagley machines                 # the machines in routing order, with their health
bagley machines --fresh         # probe them all now
bagley machines route           # which machine and model would answer now
bagley machines route --purpose vision
bagley machines add H4CH1 http://h4ch1:11434 --role gpu
bagley machines add Groq https://api.groq.com/openai/v1 --role cloud --provider openai \
    --model llama-3.3-70b-versatile --key-env GROQ_API_KEY
bagley machines remove h4ch1
```

```
ROUTING AUTO // LIGHT LOCAL
#   ID     NAME   ROLE   URL                             MODELS  MODEL                    STATUS
01  h4ch1  H4CH1  GPU    http://h4ch1:11434                   7  qwen3:14b                ONLINE 12ms
02  local  B1T    LOCAL  http://localhost:11434               3  auto                     ONLINE 3ms
03  groq   GROQ   CLOUD  https://api.groq.com/openai/v1      21  llama-3.3-70b-versatile  ONLINE 180ms
```

An API key is read from the environment variable you name with `--key-env`, never from the command
line (it would end up in your shell history and the process list). `machines remove` puts routing
back on `auto` if it was pinned to that machine.

## A desktop GPU over Tailscale

The usual setup: Bagley on the laptop, Ollama on the desktop's GPU, both on the same Tailscale
network.

**On the desktop** (Kali, AMD GPU). Ollama's installer adds ROCm support for Radeon cards such as the RX 7900 XT by itself:

```sh
curl -fsSL https://ollama.com/install.sh | sh
sudo systemctl edit ollama        # add the lines below
sudo systemctl restart ollama
```

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0"
Environment="OLLAMA_KEEP_ALIVE=30m"
Environment="OLLAMA_FLASH_ATTENTION=1"
```

Ollama has no authentication: `0.0.0.0` makes it answer on every network the desktop is on. Use
the desktop's Tailscale address instead (`OLLAMA_HOST=100.x.y.z`), or allow the port only on the
Tailscale interface, e.g. `sudo ufw allow in on tailscale0 to any port 11434`.

Then pull a few models (`ollama pull qwen3:14b`) and check from the laptop:
`curl http://h4ch1:11434/api/version` (the MagicDNS name or the 100.x address).

**On the laptop:**

```sh
bagley machines add H4CH1 http://h4ch1:11434 --role gpu
bagley machines                   # H4CH1 should be ONLINE and first
bagley bench --machine h4ch1 --apply
```

When the desktop sleeps, Bagley notices that it doesn't answer, skips it for a while and answers
on the laptop; the reply says which machine produced it.

### What fits a 20 GB RX 7900 XT

Sizes are for the default Q4 builds in the Ollama library; the context (KV cache) needs room on
top, which grows with `context_tokens` (8192 by default).

| Model | Size | On 20 GB |
|---|---|---|
| `qwen3:14b` | ~9 GB | Fits with room for a long context. Reliable tool calling; a good default |
| `qwen2.5-coder:14b` | ~9 GB | Fits. For code |
| `gpt-oss:20b` | ~14 GB | Fits. Native tool calling, fast |
| `mistral-small3.2:24b` | ~15 GB | Fits with a moderate context. Also reads images |
| `qwen3:30b-a3b` | ~19 GB | Just fits with a small context, or spills a few layers to the CPU. Fast anyway: only 3B parameters are active per token |
| `qwen2.5:32b` | ~20 GB | Does not fit: partial offload to the CPU, several times slower. Worth it only when quality matters more than speed |

On the laptop keep small models for light work and offline use (`qwen3:4b`, `llama3.2:3b`). Run
`bagley bench` after pulling or removing models; the numbers on your hardware beat any table.
