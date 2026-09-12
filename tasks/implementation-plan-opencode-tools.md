# Implementation Plan: OpenCode-Tools v0.1

| Campo | Valore |
|---|---|
| Stato | Canonico per la sequenza implementativa; non attesta funzionalità già implementate |
| Release target | v0.1 |
| Data | 2026-09-12 |
| Fonte prodotto autorevole | [`tasks/prd-opencode-tools.md`](./prd-opencode-tools.md) |
| Design vincolante | [`tasks/system-design-opencode-tools.md`](./system-design-opencode-tools.md) e ADR accettati in [`docs/adr/`](../docs/adr/) |
| Scope dell'attività | Solo piano di implementazione; nessun codice, configurazione di prodotto o documento fonte modificato |

Questo documento trasforma requisiti e decisioni già approvati in milestone piccole, cumulative e verificabili. Non riesamina l'architettura: PRD, System Design e ADR restano i contratti da applicare. Quando una milestone indica che un requisito è “coperto”, significa che ne introduce il comportamento e i test; la conformità di release resta subordinata al gate finale M15.

## 1. Vincoli invarianti e regole di esecuzione

Durante tutte le milestone valgono questi vincoli non negoziabili:

- Python `>=3.13`, `src` layout, standard library come unica dipendenza Python runtime;
- Python è l'unica autorità su lifecycle, retry, review cycle, postflight, finalizzazione ed exit code;
- i soli agenti OpenCode sono `architect`, `coder` e `reviewer`, definiti project-local;
- ogni invocation accetta esattamente una issue; nessun batch, range o coda multi-issue;
- workspace OpenCode e target Git restano value object e path distinti;
- tutti i comandi sono argv strutturati con `shell=False`; il prompt OpenCode passa su stdin;
- tutti i processi, retry, backoff, review cycle e operazioni utility sono bounded;
- provider attempt e review cycle sono contatori e loop separati;
- ogni stato Git ambiguo, compatibilità non provata o agent identity non verificabile fallisce chiuso;
- nessun commit, amend, tag, branch creation, push, merge, rebase, reset, clean, stash, checkout/switch distruttivo o mutation GitHub viene costruito da Python;
- le modifiche complete o parziali restano non committate e non vengono ripristinate automaticamente;
- gli artifact sono locali, strutturati, atomici dove previsto e protetti con mode POSIX;
- le sole piattaforme di release sono macOS e Linux su filesystem locale POSIX;
- OpenCode `1.17.18` è un adapter candidato finché fixture e smoke M15 non ne qualificano il supporto; non sono ammessi fallback.

Gli SH-001–SH-007 sono inclusi perché il System Design li ha esplicitamente incorporati nella baseline v0.1. I CO-001–CO-008 e tutti i non-obiettivi PRD restano esclusi.

## 2. Strategia incrementale e quality gate comune

Ogni milestone parte dal risultato verde delle proprie dipendenze, aggiunge un solo confine coerente e lascia il repository importabile e testabile. Non si inizia la milestone successiva finché la Definition of Done della corrente non è soddisfatta.

M01 fissa questi comandi come gate cumulativo `QG`:

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

Per ogni milestone si esegue prima il comando mirato indicato e poi l'intero `QG`. I test ordinari non usano rete, credenziali, provider/modelli reali o attese di backoff reali. Lo smoke live di M15 è opt-in, disposable e separato dal gate deterministico.

### 2.1 Ordine e dipendenze

| Milestone | Risultato verificabile | Prerequisiti |
|---|---|---|
| M01 | Package importabile e quality tooling operativo | Nessuno |
| M02 | Dominio, error model e porte tipizzati e stabili | M01 |
| M03 | Richiesta, path e configurazione v1 validati senza I/O agent | M02 |
| M04 | State machine, precedence e retry policy puri | M02, M03 |
| M05 | Esecuzione processi POSIX bounded e fault-injectable | M02, M03 |
| M06 | Protocollo agent v1 strict e fail-closed | M02 |
| M07 | Adapter OpenCode `1.17.18` candidato, coperto da fixture offline | M03, M05, M06 |
| M08 | Runtime store, `run.json` e attempt log atomici/privati | M02, M03, M05 |
| M09 | Target Git validato, fingerprint e checkpoint safety verificabili | M02, M03, M05 |
| M10 | Bootstrap runtime, lock target-scoped e quarantine operativi | M03, M05, M08, M09 |
| M11 | Identity GitHub, prompt e tre agent definition verificati | M03, M06, M07, M09 |
| M12 | Una logical invocation completa con retry/checkpoint/logging | M04–M11 |
| M13 | Pipeline single-issue completa con fake deterministici | M12 |
| M14 | CLI/composition root e documentazione operatore complete | M01–M13 |
| M15 | Tutti gli acceptance e la qualification di release verificati | M14 |

La catena critica è `M01 → M02 → M03 → M05 → M07 → M11 → M12 → M13 → M14 → M15`. M04, M06, M08, M09 e M10 avanzano appena disponibili i rispettivi contratti, senza introdurre dipendenze inverse.

## 3. Milestone implementative

### M01 — Bootstrap del package e quality baseline

**Obiettivo.** Rendere il repository un package Python 3.13+ importabile, con tool di sviluppo riproducibili e nessuna dipendenza runtime di terze parti.

**Prerequisiti.** Nessuno.

**Componenti/file.**

- `pyproject.toml`
- `src/opencode_tools/__init__.py`
- `.gitignore`
- `README.md` (solo setup e comandi quality iniziali)
- `tests/unit/test_package.py`
- `tests/unit/test_import_boundaries.py` (scaffold del controllo architetturale)

**Comportamento implementato.**

- metadata del package e `requires-python = ">=3.13"`;
- nessuna voce di dipendenza runtime Python;
- dipendenze di sviluppo limitate a pytest, Ruff e mypy;
- versione package esposta da `opencode_tools.__version__`;
- predisposizione di `.opencode-tools/` fra gli artifact locali ignorati del repository, senza attribuire al programma alcun auto-edit delle ignore rule;
- configurazione strict di mypy e configurazione coerente di pytest/Ruff;
- documentazione degli esatti comandi `QG`.

**Test unitari.** Import/versione del package, requisito Python e audit del manifest senza runtime dependencies.

**Test di integrazione.** Nessuno.

**Requisiti PRD coperti.** NFR-001–NFR-004; base per FR-015 e AC-026.

**ADR applicabili.** ADR-001, ADR-009.

**Definition of Done.** Il package è importabile da `src`, il manifest non dichiara package runtime, i quattro quality command sono documentati ed eseguibili, lo scaffold del test import-boundary è verde.

**Validazione prevista.**

```bash
PYTHONPATH=src python3.13 -m pytest tests/unit/test_package.py tests/unit/test_import_boundaries.py
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

### M02 — Domain model, error records e ports

**Obiettivo.** Congelare i contratti typed e immutabili sui quali dipenderanno policy e adapter, senza I/O o lifecycle implicito.

**Prerequisiti.** M01.

**Componenti/file.**

- `src/opencode_tools/domain.py`
- `src/opencode_tools/errors.py`
- `src/opencode_tools/ports.py`
- `tests/unit/test_domain.py`
- `tests/unit/test_errors.py`
- `tests/unit/test_ports.py`
- `tests/unit/test_import_boundaries.py`

**Comportamento implementato.**

- `StrEnum` stabili per phase, role, agent/review/final status, outcome, Git safety e persistenza;
- value object frozen/slotted per workspace, target, repository/issue identity, process/agent/Git/attempt/error/run/result records;
- phase, outcome, status, Git safety, persistenza e final status restano dimensioni separate e nullable solo dove previsto;
- `PREFLIGHT_ERROR` e `INTERRUPTED` sono inclusi come estensioni già approvate dal System Design;
- `Protocol` per process runner, clock, sleeper, attempt sink, agent runner, Git safety, issue resolver, run store e lease factory;
- eccezioni interne typed convertibili in `ErrorRecord`, senza decisioni di retry/finalizzazione;
- regola d'import: il dominio non importa moduli applicativi e nessun modulo low-level dipende dall'orchestrator.

**Test unitari.** Valori enum, invarianti costruttive, immutabilità, tuple/copie difensive, campi nullable, round-trip verso primitive JSON, Protocol sostituibili e import graph consentito/vietato.

**Test di integrazione.** Nessuno.

**Requisiti PRD coperti.** FR-010, FR-024, FR-027, FR-034, FR-045; NFR-003, NFR-006–NFR-007, NFR-011–NFR-012.

**ADR applicabili.** ADR-001, ADR-002, ADR-004, ADR-008.

**Definition of Done.** Ogni stato canonico è rappresentabile senza enum omnicomprensivi; le porte accettano fake senza import concreti; i test impediscono dipendenze low-level verso `orchestrator.py`.

**Validazione prevista.**

```bash
python3.13 -m pytest tests/unit/test_domain.py tests/unit/test_errors.py tests/unit/test_ports.py tests/unit/test_import_boundaries.py
ruff check .
ruff format --check .
mypy --strict src tests
```

### M03 — Run request, path resolution e configurazione v1

**Obiettivo.** Produrre un `RunRequest` e un `AppConfig` completi e validati prima di qualsiasi agent o creazione di artifact.

**Prerequisiti.** M02.

**Componenti/file.**

- `src/opencode_tools/config.py`
- aggiornamenti mirati a `src/opencode_tools/domain.py` e `src/opencode_tools/errors.py`
- `tests/unit/test_config.py`
- `tests/unit/test_paths.py`
- `tests/fixtures/config/` per TOML golden/invalidi

**Comportamento implementato.**

- issue come singolo intero positivo; workspace `resolve(strict=True)` e target solo `.` o path relativo;
- target canonicalizzato e contained nel workspace dopo symlink resolution; la prova top-level Git resta a M09;
- `--config` relativo concettualmente al cwd di invocazione; lookup convenzionale `<workspace>/opencode-tools.toml`; altrimenti default built-in;
- schema TOML chiuso e versionato `version = 1`, con sezioni e default esatti del System Design;
- validazione di tipi/range, bool esclusi dai numeri, NaN/infinito rifiutati, chiavi sconosciute/duplicate rifiutate;
- `runtime.root` relativo al workspace o assoluto canonicalizzato, mai sotto Git metadata; la verifica ignore/ownership/mode resta a M10;
- override `github.targets` target-specifici con almeno `remote` o `repository`;
- nessun model/provider ID, environment override o lifecycle override CLI individuale;
- configurazione effettiva sanitizzata con source `explicit`, `conventional` o `defaults`.

I valori congelati da implementare sono:

| Chiave | Default | Range/vincolo |
|---|---:|---|
| `execution.opencode_timeout_seconds` | 1800 | finito, `1..7200` |
| `execution.utility_timeout_seconds` | 30 | finito, `1..300` |
| `execution.termination_grace_seconds` | 5 | finito, `0.1..60` |
| `execution.max_review_cycles` | 3 | intero, `1..20` |
| `provider_retry.max_attempts` | 3 | intero, `1..10`, primo incluso |
| `provider_retry.initial_delay_seconds` | 2 | finito, `0.1..300` |
| `provider_retry.multiplier` | 2.0 | finito, `>1.0` e `<=10.0` |
| `provider_retry.max_delay_seconds` | 30 | finito, `>= initial_delay` e `<=1800` |
| `runtime.root` | `.opencode-tools` | path non vuoto, relativo al workspace o assoluto validato |

**Test unitari.** Default, file esplicito/convenzionale, precedence, tutti i limiti e cross-field, file mancante, version mismatch, schema chiuso, bool/non-finite, target assoluto, traversal/symlink escape, chiavi target duplicate, assenza di model ID.

**Test di integrazione.** Fixture filesystem temporanei per cwd/workspace/config/runtime resolution, senza Git o subprocess agent.

**Requisiti PRD coperti.** FR-001, FR-003, FR-006–FR-009; parte path di FR-004; NFR-005, NFR-008. Validation point: AC-002 e parte AC-001, AC-004, AC-023.

**ADR applicabili.** ADR-003, ADR-007, ADR-008, ADR-010.

**Definition of Done.** Ogni config valida produce lo stesso value object a parità di input; ogni input non riconosciuto fallisce prima di agent/artifact; i default sono documentati e coincidono col System Design.

**Validazione prevista.**

```bash
python3.13 -m pytest tests/unit/test_config.py tests/unit/test_paths.py
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

### M04 — State machine, precedence e retry policy pure

**Obiettivo.** Rendere tutte le decisioni di lifecycle verificabili come funzioni pure, senza processi, Git, sleep o parsing raw.

**Prerequisiti.** M02, M03.

**Componenti/file.**

- `src/opencode_tools/state_machine.py`
- `src/opencode_tools/retry.py`
- aggiornamenti ai record in `src/opencode_tools/domain.py`
- `tests/unit/test_state_machine.py`
- `tests/unit/test_retry.py`
- `tests/unit/test_final_gate.py`

**Comportamento implementato.**

- transizioni complete `PREFLIGHT → ARCHITECT → CODER(n) → REVIEWER(n) → POSTFLIGHT → FINALIZATION → FINISHED`;
- `READY`, `COMPLETED`, `APPROVED`, `CHANGES_REQUIRED` e tutti i terminal outcome producono azioni tipizzate;
- review cycle da 1, incremento solo dopo `CHANGES_REQUIRED`, exhaustion senza coder ulteriore;
- provider attempt da 1 per ogni logical invocation, retry nello stesso phase/role/cycle;
- formula di backoff capped, decisione retry con tutti i guard canonici e nessun jitter;
- precedence per attempt e pipeline/finalizzazione, preservando cause concorrenti;
- gate `APPROVED` solo con reviewer approval, postflight `SAFE`, branch/HEAD invariati e persistenza `OK`;
- mapping totale degli outcome verso final status/exit family e funzione per il bound teorico configurato.

**Test unitari.** Tabella esaustiva delle transizioni valide/invalidhe, limite cycle 1 e N, retry/exhaustion, delay esatti/capped, counter indipendenti, guard coder mutation, cause concorrenti, gate final e bound massimo.

**Test di integrazione.** Nessuno; fake orchestration in M12–M13.

**Requisiti PRD coperti.** FR-016, FR-024–FR-030, FR-034, FR-047–FR-049; NFR-005–NFR-007, NFR-011–NFR-012; SH-004. Validation point unitario per AC-009–AC-012, AC-019 e AC-025.

**ADR applicabili.** ADR-001, ADR-002, ADR-004.

**Definition of Done.** Nessuna funzione del modulo esegue I/O o interpreta testo; a parità di eventi il trace è identico; nessun retry modifica il review cycle e nessun rework modifica il provider attempt precedente.

**Validazione prevista.**

```bash
python3.13 -m pytest tests/unit/test_state_machine.py tests/unit/test_retry.py tests/unit/test_final_gate.py
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

### M05 — ProcessRunner POSIX bounded

**Obiettivo.** Eseguire qualunque child locale con timeout, drain e terminazione verificabili, senza conoscenza di OpenCode, Git o protocollo agent.

**Prerequisiti.** M02, M03.

**Componenti/file.**

- `src/opencode_tools/process.py`
- aggiornamenti ai contratti in `src/opencode_tools/ports.py`, `domain.py`, `errors.py`
- `tests/unit/test_process_results.py`
- `tests/component/test_process_runner.py`
- helper child locali sotto `tests/component/helpers/`

**Comportamento implementato.**

- `subprocess.Popen` con argv tuple, `shell=False`, path eseguibile assoluto e cwd per child; nessun `os.chdir()`;
- stdin separato dall'argv, environment ereditato ma mai enumerato/persistito;
- drain concorrente e bounded di stdout/stderr verso sink/observer senza accumulo illimitato;
- timestamp UTC, durata da `monotonic_ns`, byte count/digest e return code nullable;
- `start_new_session=True` e sequenza `SIGTERM → grace → SIGKILL → grace`, inclusi processi discendenti;
- ritorno bounded con `termination_confirmed = false` se process group/pipe/reader restano incerti;
- spawn failure ed exit non-zero tecnici distinti; output parziale preservato;
- sink aperto prima dello spawn; failure del sink termina il child e propaga `LOGGING_ERROR`;
- gestione SH-001: richiesta di cancellazione SIGINT/SIGTERM con la stessa escalation bounded.

**Test unitari.** Mapping `ProcessResult`, deadline/clock fake, return code nullable, comando sanitizzato ed environment non serializzato.

**Test di integrazione.** stdout/stderr simultanei voluminosi, stdin, spawn assente, exit non-zero, timeout, child con discendente, TERM-resistant, partial output, reader/sink fault e terminazione non confermata simulata al boundary.

**Requisiti PRD coperti.** FR-032–FR-034 e parte processuale di FR-052; NFR-002, NFR-005–NFR-007, NFR-010; SH-001. Validation point: AC-014, AC-015, parte AC-032–AC-034.

**ADR applicabili.** ADR-004, ADR-008, ADR-009, ADR-010.

**Definition of Done.** Ogni child termina o restituisce controllo entro il bound; nessun deadlock su pipe; `process.py` non importa `opencode.py`, Git, protocollo o orchestrator; i test non attendono timeout reali lunghi.

**Validazione prevista.**

```bash
python3.13 -m pytest tests/unit/test_process_results.py tests/component/test_process_runner.py
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

### M06 — Agent protocol v1 fail-closed

**Obiettivo.** Validare deterministicamente il solo messaggio assistant terminale già ricostruito, senza leggere NDJSON o altri canali.

**Prerequisiti.** M02.

**Componenti/file.**

- `src/opencode_tools/protocol.py`
- aggiornamenti ai tipi issue/response in `src/opencode_tools/domain.py`
- `tests/unit/test_protocol.py`
- `tests/fixtures/protocol/`

**Comportamento implementato.**

- `protocol.py` riceve un solo testo assistant già decodificato/normalizzato; l'adapter M07 applica UTF-8 strict e la sola normalizzazione CRLF/CR→LF prima del parser;
- marker exact, a colonna zero, unico e terminale; prefissi riservati e `FINAL_STATUS` agent rifiutati;
- matrice status per ruolo esatta e `CHANGES_REQUIRED` come successo tecnico;
- body opaco preservato; non vuoto per architect READY, ogni FAILED e CHANGES_REQUIRED;
- `ISSUE_REF_JSON` v1 single-line, schema chiuso di sette campi, immediatamente prima di architect READY;
- tipi JSON strict con bool rifiutati, stringhe difensive, identity/URL HTTPS canonici confrontati col locator;
- nessuna euristica, code-fence extraction, ricerca in issue/tool/stderr o recupero di output quasi valido.

**Test unitari.** Matrice ruolo/status, body vuoto/non vuoto, newline terminali, casing/spazi/suffissi, duplicate/conflict/nonterminal, prefissi unknown, `FINAL_STATUS`, envelope valido e ogni forma malformed/mismatch/extra/missing/type/URL.

**Test di integrazione.** Nessuno; il boundary NDJSON arriva in M07.

**Requisiti PRD coperti.** FR-017–FR-018, FR-024, FR-035–FR-038, FR-047; NFR-008, NFR-011–NFR-012. Validation point: AC-016, AC-017, parte AC-029 e AC-035.

**ADR applicabili.** ADR-002, ADR-007, ADR-010.

**Definition of Done.** Il parser accetta soltanto tutte e sole le forme canoniche; il body non guida lifecycle/classifier; qualunque ambiguità produce `PROTOCOL_ERROR` typed.

**Validazione prevista.**

```bash
python3.13 -m pytest tests/unit/test_protocol.py
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

### M07 — Adapter OpenCode 1.17.18 candidato e fixture offline

**Obiettivo.** Isolare trasporto, capability, provider classifier e agent identity di OpenCode dietro un adapter exact-version, senza ancora dichiarare qualificata la release.

**Prerequisiti.** M03, M05, M06.

**Componenti/file.**

- `src/opencode_tools/opencode.py`
- `tests/unit/test_opencode_adapter.py`
- `tests/unit/test_provider_classifier.py`
- `tests/component/test_opencode_preflight.py`
- `tests/fixtures/opencode/1.17.18/`
- `docs/compatibility.md` (stato iniziale: candidato, non ancora qualificato)

**Comportamento implementato.**

- executable risolto una volta; `opencode --version` singolo e match esatto dell'adapter;
- capability preflight su `run --help`, `debug config`, `debug agent` per tre role primary e permission baseline;
- digest canonico del control plane e recheck bounded prima di ogni `run`;
- command builder esatto `run --agent <role> --format json --dir <workspace>`, cwd workspace, prompt su stdin;
- assenza di `--auto`, `--share`, `--model`, session/continue/fork/attach e opzioni provider; auto-share e auto-update disabilitati tramite override non segreti;
- decode UTF-8 strict e sola normalizzazione CRLF/CR→LF, quindi parser NDJSON `1.17.18` con schema/eventi/session ID/dimensioni bounded, terminal assistant text unico e tool/reasoning esclusi;
- prova agent tramite `export <session-id> --sanitize`, raw non persistito e `info.agent` coerente;
- classifier provider soltanto da `session.error`/campi coperti da fixture per 429, 502, unavailable, overload e rate limiting;
- nessun fallback su versione, testo, stderr, default agent o classifier incerto;
- fixture immutabili con provenance per success/failure, tutte le signature, lookalike non trusted, malformed/truncated, multi-terminal, fallback e identity/config/export.

**Test unitari.** Command/env sanitizer, version registry, NDJSON grouping, limiti, provider trust boundary/precedence, session export e control-plane digest.

**Test di integrazione.** Fake executable OpenCode per call count/version/capability/debug/export, fallback warning, agent mismatch e drift digest.

**Requisiti PRD coperti.** FR-005 lato OpenCode, FR-009, FR-011–FR-012, FR-018, FR-021–FR-022, FR-028, FR-035; NFR-008–NFR-009, NFR-011; SH-005. Validation point: AC-007, AC-016, AC-023–AC-024, AC-031, AC-035; precondizione di AC-027.

**ADR applicabili.** ADR-002, ADR-004, ADR-005, ADR-009, ADR-010.

**Definition of Done.** Tutte le fixture offline passano; versione/agent/output incerti falliscono chiuso; nessun model ID è in Python/TOML; `docs/compatibility.md` non presenta ancora `1.17.18` come supportata finché M15 non supera lo smoke.

**Validazione prevista.**

```bash
python3.13 -m pytest tests/unit/test_opencode_adapter.py tests/unit/test_provider_classifier.py tests/component/test_opencode_preflight.py
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

### M08 — Runtime store e logging strutturato

**Obiettivo.** Rendere ogni run inizializzata e ogni attempt ricostruibili tramite una source of truth atomica e log distinti, privati e non sovrascritti.

**Prerequisiti.** M02, M03, M05.

**Componenti/file.**

- `src/opencode_tools/runlog.py`
- aggiornamenti ai record/port in `domain.py` e `ports.py`
- `tests/unit/test_run_schema.py`
- `tests/unit/test_runlog_naming.py`
- `tests/component/test_runtime_store.py`

**Comportamento implementato.**

- run ID UTC microsecondi + 12 hex random e directory `runs/<run-id>` creata exclusive con retry collisione bounded;
- layout e naming canonici dei log per role/cycle/attempt, apertura non-truncate/exclusive;
- `run.json` schema v1 con input, config sanitizzata, environment, timing, state, Git, timeline, attempts, errors, persistence e result come record ortogonali;
- timestamp RFC 3339 UTC, durate `duration_ns`, sequence monotone e JSON UTF-8 deterministico;
- temp same-directory imprevedibile `0600`, write completa, flush/fsync, close, `os.replace`, fsync directory quando supportato;
- fallimento lascia intatta l'ultima JSON valida, interrompe nuove invocation e produce `LOGGING_ERROR`/artifact incomplete;
- attempt log line-framed append-only con timestamp/canale/payload escaped, header/footer, byte digest e flush/fsync;
- directory `0700`, file `0600`; niente environment completo, credential, raw auth/config/export, prompt nel comando o model/provider ID;
- nessuna retention/rotation/prune automatica.

**Test unitari.** Schema/golden, nullable e cause multiple, serialization stabile, naming cycle nullable, sanitizer, collisioni e run ID.

**Test di integrazione.** Mode POSIX, symlink/path safety di apertura, open/write/serialize/fsync/replace fault injection, ultima JSON valida, log collision/truncation e sink failure collegata al runner.

**Requisiti PRD coperti.** FR-042–FR-046, FR-052; NFR-007–NFR-008, NFR-010–NFR-011. Validation point: AC-021, AC-025 lato persistenza, AC-033–AC-034.

**ADR applicabili.** ADR-004, ADR-008, ADR-009, ADR-010.

**Definition of Done.** Ogni write completata lascia JSON valido; nessun tentativo sovrascrive un log; una failure di logging impedisce la prossima invocation; i permessi non superano 0700/0600.

**Validazione prevista.**

```bash
python3.13 -m pytest tests/unit/test_run_schema.py tests/unit/test_runlog_naming.py tests/component/test_runtime_store.py
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

### M09 — Git target safety e fingerprint `git-state-v1`

**Obiettivo.** Validare e sorvegliare esclusivamente il target repository con probe read-only, snapshot content-sensitive e failure fail-closed.

**Prerequisiti.** M02, M03, M05.

**Componenti/file.**

- `src/opencode_tools/git_safety.py`
- aggiornamenti ai tipi/port in `domain.py` e `ports.py`
- `tests/unit/test_git_fingerprint.py`
- `tests/unit/test_git_commands.py`
- `tests/component/test_git_repository.py`
- `tests/component/test_git_checkpoints.py`

**Comportamento implementato.**

- target canonicalizzato contained nel workspace e uguale esattamente al Git top-level non bare;
- tutti i comandi Git read-only con `git -C <target>`, deadline utility e nessun cwd implicito;
- branch attached, `HEAD^{commit}` risolvibile e baseline sempre clean senza bypass dirty;
- `git-state-v1`: SHA-256 di record length-prefixed ordinati che coprono porcelain raw, index, tracked/untracked non ignorati, path raw, tipo, contenuto/symlink ed executable bit;
- path non UTF-8 byte-safe, file speciali/escape/permission error `INDETERMINATE`;
- lstat prima/dopo, ricampionamento finale e un solo retry bounded su instabilità;
- snapshot immediatamente prima/dopo ogni attempt e continuità con l'ultimo checkpoint accettato;
- branch/HEAD invarianti; delta architect/reviewer unsafe; delta coder riuscito inventariato; delta coder con provider error disponibile al retry guard;
- inventario staged, tracked-unstaged e untracked per reviewer/sommario, senza copie del codice in `run.json`;
- postflight best effort e preservation; nessun reset/clean/stash/checkout/rollback o altro command mutativo.

**Test unitari.** Command allowlist, framing/hash/versione, path byte-safe, type/mode/content, same-porcelain second edit, summary/inventory e mapping probe outcome→GitSafetyStatus.

**Test di integrazione.** Repo temporanei clean/staged/unstaged/untracked/ignored, bare/unborn/detached/nested, symlink escape, branch/HEAD drift, probe timeout/non-zero/ambiguous, race secondo campionamento, mutation read-only-role e coder partial mutation.

**Requisiti PRD coperti.** FR-003–FR-005, FR-013–FR-014, FR-020, FR-022–FR-023, FR-031, FR-039–FR-041, FR-050–FR-051; NFR-005, NFR-008, NFR-011; SH-007. Validation point: AC-003–AC-006, AC-013, AC-018–AC-020, AC-028, AC-030 e AC-036.

**ADR applicabili.** ADR-003, ADR-004, ADR-009, ADR-010.

**Definition of Done.** Nessun Git command può operare sul workspace per implicazione o mutare stato; una seconda modifica a un file già `M` cambia fingerprint; ogni probe incerto impedisce approval; lo stato utente viene preservato.

**Validazione prevista.**

```bash
python3.13 -m pytest tests/unit/test_git_fingerprint.py tests/unit/test_git_commands.py tests/component/test_git_repository.py tests/component/test_git_checkpoints.py
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

### M10 — Runtime bootstrap, target lock e quarantine

**Obiettivo.** Rendere sicuri creazione artifact e mutua esclusione sul checkout prima della baseline e dopo termination incerta.

**Prerequisiti.** M03, M05, M08, M09.

**Componenti/file.**

- `src/opencode_tools/locking.py`
- integrazioni mirate in `git_safety.py`, `runlog.py`, `ports.py`
- `tests/unit/test_runtime_bootstrap.py`
- `tests/component/test_locking.py`
- `tests/component/test_runtime_location.py`

**Comportamento implementato.**

- runtime root canonicale: se dentro un working tree, directory e sentinel devono essere già ignorati prima di crearli; sotto Git metadata è sempre rifiutata;
- root esistente owner corrente, non symlink e senza bit group/other; nessun chmod/chown/ignore auto-correctivo;
- platform/filesystem fuori baseline POSIX locale falliscono chiuso;
- coordination dir sotto `git rev-parse --absolute-git-dir`, mode 0700; lock/quarantine 0600;
- `fcntl.flock(LOCK_EX|LOCK_NB)` target-scoped, metadata scritti solo dopo acquisizione, nessuna stale heuristic PID/tempo;
- target differenti hanno lease distinti; stesso target fallisce `PREFLIGHT_ERROR/TARGET_LOCKED` senza agent;
- lease previsto da prima della baseline fino a dopo finalizzazione;
- `quarantine-v1.json` atomica prima del rilascio quando `termination_confirmed=false`; blocco persistente e nessuna rimozione automatica.

**Test unitari.** Derivazione coordination path, metadata sanitizzati, decisione lock/quarantine e platform gate.

**Test di integrazione.** Due processi sullo stesso target, target diversi, crash/release kernel, file lock residuo innocuo, filesystem lock failure, quarantine bloccante, runtime inside/outside/ignored/unignored, symlink/owner/mode e failure di scrittura quarantine.

**Requisiti PRD coperti.** FR-015, FR-033 lato quarantine, FR-039–FR-042; NFR-005, NFR-008, NFR-010; SH-002. Validation point: AC-022, AC-032 e AC-034.

**ADR applicabili.** ADR-003, ADR-004, ADR-006, ADR-008, ADR-009.

**Definition of Done.** Due run conformi non possono condividere lo stesso checkout; runtime root diverse non aggirano il lock; termination incerta rende esplicitamente necessaria una recovery manuale; nessuna safety rule viene modificata automaticamente.

**Validazione prevista.**

```bash
python3.13 -m pytest tests/unit/test_runtime_bootstrap.py tests/component/test_locking.py tests/component/test_runtime_location.py
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

### M11 — GitHub identity, prompting e agent definition

**Obiettivo.** Collegare la singola issue al solo target e rendere verificabili input, ruolo, write scope e permission policy dei tre agenti.

**Prerequisiti.** M03, M06, M07, M09.

**Componenti/file.**

- `src/opencode_tools/github.py`
- `src/opencode_tools/prompting.py`
- `.opencode/agents/architect.md`
- `.opencode/agents/coder.md`
- `.opencode/agents/reviewer.md`
- `tests/unit/test_github_identity.py`
- `tests/unit/test_prompting.py`
- `tests/unit/test_agent_policy.py`
- `tests/component/test_github_boundary.py`

**Comportamento implementato.**

- resolver target-specifico sui soli fetch URL: repository override → remote esplicito → `origin` valido → unica identity GitHub; mismatch/ambiguità falliscono;
- parsing HTTPS/SSH/scp-like senza credential/query/fragment persistiti; GitHub.com ed Enterprise autenticato;
- preflight bounded `gh --version` e `gh auth status --hostname`, raw auth non persistito;
- Python crea soltanto `IssueLocator`, non legge title/body e non usa API GitHub native;
- prompt architect con `gh issue view <number> --repo <identity>`, target/workspace espliciti e body dichiarato non fidato;
- prompt coder con issue ref, handoff opaco integrale, target unico, cycle e policy;
- prompt reviewer con issue/handoff, report coder, inventario/diff/test scope e target read-only; feedback trasportato byte-for-byte dopo la sola normalizzazione prevista;
- delimiter trusted e nessuna interpretazione/riassunto Python di issue, handoff o feedback;
- tre agent primary, nessun `orchestrator`, `ask` o `task`; modelli solo nei file agent e sostituibili fra run;
- permission matrix minima: architect/reviewer read-only, coder edit solo target; negati staging e tutte le mutation Git/GitHub/share canoniche.

**Test unitari.** Remote normalization/precedence/ambiguity, enterprise, sanitizer URL, prompt golden per ruolo/cycle, opaque payload, issue marker injection, policy allow/deny completa, assenza model ID dal package/TOML.

**Test di integrazione.** Fake Git/gh per origin/override/auth/not-found; fake debug-agent per effective policy; workspace Backend/Frontend; cambio dei tre model ID nei file agent senza modifica Python/TOML.

**Requisiti PRD coperti.** FR-005, FR-010, FR-016–FR-023; NFR-008, NFR-011. Validation point: AC-023–AC-024, AC-029–AC-030.

**ADR applicabili.** ADR-001–ADR-003, ADR-005, ADR-007, ADR-010.

**Definition of Done.** Nessun cwd/workspace ambiguo seleziona la repository; Python non legge la issue; i prompt contengono tutti gli handoff senza interpretarli; la matrice statica/effective nega ogni azione proibita; cambiare modelli non cambia Python/TOML.

**Validazione prevista.**

```bash
python3.13 -m pytest tests/unit/test_github_identity.py tests/unit/test_prompting.py tests/unit/test_agent_policy.py tests/component/test_github_boundary.py
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

### M12 — Logical invocation engine

**Obiettivo.** Integrare una singola esecuzione logica di un ruolo, inclusi provider attempt, snapshot, process result, protocol, retry e persistenza, senza ancora comporre l'intera issue pipeline.

**Prerequisiti.** M04–M11.

**Componenti/file.**

- prima implementazione di `src/opencode_tools/orchestrator.py`
- integrazioni minime in `state_machine.py`, `retry.py`, `prompting.py`
- `tests/unit/test_attempt_precedence.py`
- `tests/component/test_orchestrator_attempts.py`

**Comportamento implementato.**

- orchestrator dipendente solo dai port iniettati, senza adapter concreti o subprocess diretti;
- logical invocation ID e provider attempt separati dal cycle;
- sequenza obbligatoria: log exclusive, snapshot before, control-plane recheck, agent run, snapshot/check after, precedence, protocol se ammesso, persist, retry/stop;
- snapshot-before continuo con l'ultimo checkpoint accettato e Git check anche su spawn/provider/timeout/protocol failure;
- record distinto e non sovrascritto per ogni attempt, con classifier source/signature e delay planned/actual;
- retry soltanto con tutti i guard; provider exhaustion senza consumo cycle; coder mutation sopprime retry e preserva output;
- logging failure interrompe nuove invocation e termina un eventuale child; cancellation impedisce attempt successivi;
- outcome tecnico e Git/persistence status restano separati con error records causali.

**Test unitari.** Precedence completa, guard matrix, sequence number, retry decision prima/dopo sleep, cause concorrenti e nessun parsing dopo outcome a priorità superiore.

**Test di integrazione.** `ScriptedAgentRunner`, fake clock/sleeper/Git/store/lease per provider recovery/exhaustion, timeout/process/protocol/agent failure, coder partial mutation, read-only mutation, drift fra attempt, logging failure e interrupt.

**Requisiti PRD coperti.** FR-027–FR-041, FR-043–FR-046, FR-050–FR-052; NFR-005–NFR-008, NFR-011–NFR-012; SH-001. Validation point: AC-011–AC-018, AC-021, AC-028, AC-031–AC-033, AC-036 a livello invocation.

**ADR applicabili.** ADR-001–ADR-006, ADR-008–ADR-010.

**Definition of Done.** Ogni logical invocation produce un trace deterministico completo; nessun retry non-provider è possibile; Git/logging failure prevalgono senza cancellare il trigger; nessun test richiede OpenCode/provider/rete o sleep reale.

**Validazione prevista.**

```bash
python3.13 -m pytest tests/unit/test_attempt_precedence.py tests/component/test_orchestrator_attempts.py
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

### M13 — Pipeline single-issue end-to-end con fake

**Obiettivo.** Comporre una sola issue attraverso preflight, tre ruoli, review loop, postflight e finalizzazione, mantenendo il lease per l'intera finestra critica.

**Prerequisiti.** M12.

**Componenti/file.**

- completamento di `src/opencode_tools/orchestrator.py`
- aggiornamenti alle transition/finalization policy in `state_machine.py`
- `tests/component/test_single_issue_pipeline.py`
- `tests/component/test_pipeline_failures.py`
- `tests/component/test_pipeline_multirepo.py`

**Comportamento implementato.**

- bootstrap dopo init, lock prima della baseline e rilascio dopo finalizzazione;
- preflight completo di platform, runtime, Git, GitHub, OpenCode/capability/effective agents/versione una volta;
- ordine diretto `architect → coder(1) → reviewer(1)` e successivi `coder(n) → reviewer(n)` soltanto dopo `CHANGES_REQUIRED`;
- architect READY richiesto prima del coder; issue envelope validato; handoff e feedback integrali;
- reviewer retry nello stesso ciclo senza richiamare coder; exhaustion senza coder extra;
- ogni terminal path inizializzato converge a postflight best effort e finalizzazione;
- final approval solo col gate completo; `APPROVED` storico non prevale su drift/persistence failure;
- preservation e change inventory su successo/fallimento; termination incerta crea quarantine;
- esattamente un final status nel risultato Python, senza ancora occuparsi del rendering CLI.

**Test unitari.** Nessuno nuovo oltre alle policy pure; eventuali regressioni devono essere aggiunte al modulo owner, non compensate nell'orchestrator.

**Test di integrazione.** Happy path, rework poi approval, review exhaustion, provider recovery/exhaustion per ogni ruolo, coder partial mutation, tutti i terminal outcome, approval-then-drift, postflight failure, multi-repo, control-plane drift, lock/quarantine, persistence failure e interrupt.

**Requisiti PRD coperti.** FR-005, FR-010–FR-052 come composizione, con focus FR-016, FR-019, FR-025–FR-031, FR-039–FR-041, FR-047–FR-050; NFR-005–NFR-012. Validation point: AC-003, AC-007–AC-013, AC-017–AC-021, AC-025, AC-028–AC-033, AC-036.

**ADR applicabili.** ADR-001–ADR-010.

**Definition of Done.** Con fake deterministici la pipeline copre ogni transition e failure path, invoca sempre un solo issue locator, non crea un quarto agent, non effettua mutation e produce un `IssueResult` coerente con l'ultima `run.json` valida.

**Validazione prevista.**

```bash
python3.13 -m pytest tests/component/test_single_issue_pipeline.py tests/component/test_pipeline_failures.py tests/component/test_pipeline_multirepo.py
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

### M14 — CLI, composition root e documentazione operatore

**Obiettivo.** Esporre la pipeline come comando locale non interattivo e documentare uso, safety, privacy, recovery e compatibilità senza ampliare lo scope.

**Prerequisiti.** M01–M13.

**Componenti/file.**

- `src/opencode_tools/cli.py`
- `src/opencode_tools/__main__.py`
- entry point in `pyproject.toml`
- `README.md`
- `docs/security-and-privacy.md`
- `docs/recovery.md`
- aggiornamento di `docs/compatibility.md`
- `tests/unit/test_cli.py`
- `tests/component/test_cli_end_to_end.py`

**Comportamento implementato.**

- `opencode-tools run --workspace <path> --target <relative|.> --issue <positive-int> [--config <path>]` con `argparse`;
- una sola issue scalare; batch/range/lista e target assoluti rifiutati; nessun override lifecycle individuale;
- `cli.py` unico composition root, costruisce adapter e inietta port; nessuna business logic in `__main__.py`;
- errori pre-init: exit argparse/config/bootstrap coerenti senza promessa di artifact/final marker;
- dopo init: esattamente un `FINAL_STATUS: APPROVED|FAILED` su stdout; dettagli e summary su stderr;
- exit code stabili 0/2/10/20/30/40/130 secondo il System Design;
- summary con run ID, phase, outcome, artifact path e modifiche staged/tracked/untracked;
- diagnosi compatibility con versione rilevata/supportata e remediation;
- documentazione esplicita su log sensibili, retention manuale, recovery senza comandi automatici, target clean, runtime ignore, macOS/Linux, single issue e azioni vietate;
- nessun modello/provider/credential configurato da Python.

**Test unitari.** Parser positivo/negativo, issue scalar, mapping exit/final, stdout count, stderr summary, pre-init eccezioni e composition wiring.

**Test di integrazione.** CLI completa con fake OpenCode/gh e repo temporanei: workspace=target e multi-repo, config source, happy/failure, artifact path, status/exit/run.json coerenti e command capture privo di mutation/flag vietati.

**Requisiti PRD coperti.** FR-001–FR-002, FR-005–FR-006, FR-009–FR-010, FR-022, FR-042, FR-046–FR-050; NFR-001–NFR-004, NFR-008–NFR-010, NFR-012; SH-003–SH-007. Validation point: AC-001, AC-003, AC-007, AC-021, AC-023–AC-026, AC-034.

**ADR applicabili.** ADR-001, ADR-004–ADR-010.

**Definition of Done.** Il comando canonico funziona end-to-end con fake; ogni run inizializzata espone un solo final marker coerente; la documentazione non promette sandbox, Windows, supporto OpenCode non qualificato, cleanup o pubblicazione automatica.

**Validazione prevista.**

```bash
python3.13 -m pytest tests/unit/test_cli.py tests/component/test_cli_end_to_end.py
PYTHONPATH=src python3.13 -m opencode_tools --help
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

### M15 — Acceptance completa e qualification di release

**Obiettivo.** Dimostrare, senza fallback o claim non provati, che la build soddisfa tutti i Must-Have e i 36 scenari di release sulle piattaforme dichiarate.

**Prerequisiti.** M14.

**Componenti/file.**

- suite sotto `tests/unit/` e `tests/component/`
- `tests/fixtures/opencode/1.17.18/`
- `tests/integration/test_opencode_1_17_18_smoke.py`
- `docs/compatibility.md`
- `README.md`, `docs/security-and-privacy.md`, `docs/recovery.md`
- registry exact-version in `src/opencode_tools/opencode.py`, modificata per qualificare `1.17.18` solo dopo il gate

**Comportamento implementato.** Nessuna feature nuova: si chiudono esclusivamente gap di prova o difetti emersi contro contratti già congelati.

**Test unitari.** Riesecuzione completa e review della copertura FR/NFR; replay identico delle fixture; audit statico import/command/model/dependency.

**Test di integrazione.** Tutti i test AC-001–AC-036 della matrice §6.3; suite deterministica su macOS e Linux; smoke opt-in con OpenCode `1.17.18` esatto, target disposable clean, agent fixture locali, sharing disabilitato, artifact ispezionati e nessuna publication/mutation GitHub.

**Requisiti PRD coperti.** Gate di release per FR-001–FR-052, NFR-001–NFR-012, US-001–US-013, AC-001–AC-036 e SH-001–SH-007.

**ADR applicabili.** ADR-001–ADR-010, in particolare il gate condizionale ADR-005.

**Definition of Done.** Tutte le righe delle matrici §6 sono PASS con evidenza; `QG` passa su macOS e Linux; fixture hanno provenance; lo smoke passa e solo allora `1.17.18` è elencata/abilitata come supportata. Se identity proof o smoke falliscono, la release resta bloccata, l'adapter non viene reso permissivo e ADR-005 deve essere riesaminato.

**Validazione prevista.**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
python3.13 -m pytest tests/integration/test_opencode_1_17_18_smoke.py -m live
```

Lo smoke live non sostituisce e non indebolisce la suite offline; viene eseguito separatamente su un ambiente esplicitamente preparato e non è autorizzato a pubblicare, committare o mutare GitHub.

## 4. Tracciabilità dei Must-Have funzionali

Ogni riga assegna almeno una milestone di implementazione e un punto di verifica. M15 riesegue sempre l'evidenza cumulativa indicata.

| Requirement | Milestone di implementazione | Owner principali | Punto di verifica primario |
|---|---|---|---|
| FR-001 | M03, M14 | domain/config, CLI | AC-001, AC-002 |
| FR-002 | M14 | CLI | AC-001 |
| FR-003 | M03, M09 | domain/path, Git safety | AC-003, AC-004 |
| FR-004 | M03, M09 | path, Git safety | AC-004, AC-006 |
| FR-005 | M07, M09, M13 | OpenCode/Git adapter, orchestrator | AC-003 |
| FR-006 | M03, M14 | config, CLI | AC-002 |
| FR-007 | M03 | config | AC-002, AC-014 |
| FR-008 | M03 | config/errors | AC-002, AC-026 |
| FR-009 | M03, M07, M14 | config, OpenCode builder | AC-023 |
| FR-010 | M02, M11, M13 | domain, agent definitions, orchestrator | AC-023, AC-024 |
| FR-011 | M07, M13, M15 | OpenCode compatibility/preflight | AC-007, AC-027, AC-035 |
| FR-012 | M07, M13 | OpenCode adapter/orchestrator | AC-007 |
| FR-013 | M09, M13 | Git safety | AC-005, AC-006 |
| FR-014 | M09 | Git safety | AC-005, AC-006 |
| FR-015 | M01, M10 | repository ignore, runtime/Git bootstrap | AC-022 |
| FR-016 | M04, M11, M13 | state machine, prompting, orchestrator | AC-008–AC-010 |
| FR-017 | M06, M11, M13 | protocol, GitHub boundary, orchestrator | AC-029 |
| FR-018 | M06, M07, M11 | protocol, classifier, prompting | AC-016, AC-031, AC-035 |
| FR-019 | M11, M13 | prompting, orchestrator | AC-009, AC-030 |
| FR-020 | M09, M11, M13 | Git safety, agent policy, orchestrator | AC-024, AC-028 |
| FR-021 | M07, M11 | effective permission check, agent definitions | AC-024 |
| FR-022 | M05, M07, M09, M11, M14 | process/command builders, policy, CLI | AC-023, AC-024 |
| FR-023 | M09, M11, M13 | Git inventory, reviewer prompt, orchestrator | AC-030 |
| FR-024 | M02, M04, M06 | domain, state machine, protocol | AC-009, AC-010 |
| FR-025 | M04, M13 | state machine, orchestrator | AC-009, AC-010 |
| FR-026 | M04, M13 | state machine, orchestrator | AC-010 |
| FR-027 | M02, M04, M12 | domain, retry, invocation engine | AC-011, AC-021 |
| FR-028 | M07, M12 | versioned classifier, invocation engine | AC-011, AC-031 |
| FR-029 | M04, M12 | retry policy, invocation engine | AC-011 |
| FR-030 | M04, M12 | retry/state machine | AC-012 |
| FR-031 | M09, M12 | Git fingerprint, invocation engine | AC-013 |
| FR-032 | M05, M12 | process runner, invocation engine | AC-014 |
| FR-033 | M05, M10, M12, M13 | process, quarantine, orchestration | AC-014, AC-032 |
| FR-034 | M02, M04, M05, M12 | domain, precedence, process, orchestration | AC-012–AC-020, AC-033, AC-036 |
| FR-035 | M06, M07 | protocol boundary, OpenCode adapter | AC-016, AC-035 |
| FR-036 | M06 | protocol parser | AC-016, AC-035 |
| FR-037 | M02, M06 | domain, protocol parser | AC-016, AC-017 |
| FR-038 | M06 | protocol parser | AC-017, AC-030 |
| FR-039 | M09, M12, M13 | Git checkpoints, orchestration | AC-018, AC-020, AC-021 |
| FR-040 | M04, M09, M13 | final gate, Git safety, pipeline | AC-018, AC-019 |
| FR-041 | M09, M13 | Git postflight, pipeline | AC-018, AC-020 |
| FR-042 | M08, M10, M14 | run store, bootstrap, CLI | AC-021, AC-025 |
| FR-043 | M08, M12 | attempt logs, invocation engine | AC-021 |
| FR-044 | M08, M12 | run store, invocation engine | AC-021, AC-033 |
| FR-045 | M02, M08, M12 | domain, run store, orchestration | AC-021, AC-032, AC-036 |
| FR-046 | M08, M14 | run store/sanitizer, documentation | AC-034 |
| FR-047 | M04, M06, M13, M14 | final gate, protocol, pipeline, CLI | AC-025, AC-035 |
| FR-048 | M04, M13, M14 | final gate, pipeline, CLI | AC-008, AC-019, AC-025 |
| FR-049 | M04, M13, M14 | finalizer, pipeline, CLI | AC-010, AC-012–AC-020, AC-025 |
| FR-050 | M09, M12, M13 | Git safety, orchestration | AC-013, AC-018 |
| FR-051 | M09, M12 | Git fingerprint, invocation engine | AC-013, AC-028 |
| FR-052 | M05, M08, M12 | process sink, run store, invocation engine | AC-033 |

**Esito di copertura FR:** 52/52 assegnati; nessun requisito funzionale resta senza owner, test o milestone di integrazione.

## 5. Tracciabilità dei Must-Have non funzionali e delle user story

### 5.1 NFR-001–NFR-012

| Requirement | Milestone di implementazione | Evidenza verificabile |
|---|---|---|
| NFR-001 | M01, M14, M15 | `requires-python >=3.13`, `src` layout, import/CLI e matrice release |
| NFR-002 | M01, M05, M07, M15 | manifest senza runtime package; soli executable esterni; dependency audit |
| NFR-003 | M01–M15 | annotazioni complete e `mypy --strict src tests` cumulativo |
| NFR-004 | M01, M15 | quattro comandi `QG` documentati e verdi |
| NFR-005 | M03–M05, M09–M10, M12–M13 | range finiti, bound teorico, deadline e fault test |
| NFR-006 | M02, M04–M05, M12–M13 | port e fake per process, clock, sleeper, sink, agent, Git, store e lease |
| NFR-007 | M02, M05, M08 | UTC RFC 3339 e `monotonic_ns` verificati con clock fake |
| NFR-008 | M03, M06–M07, M09–M10, M12–M13 | negative fixture e `INDETERMINATE`/unknown sempre fail-closed |
| NFR-009 | M07, M15 | adapter/fixture exact-version e smoke di qualification |
| NFR-010 | M05, M08, M10, M14 | 0700/0600, no dump, documentazione raw log sensibili |
| NFR-011 | M02, M04, M06–M07, M09, M12–M13 | replay identico di transizioni/classifier/backoff/fixture |
| NFR-012 | M01–M02 e review M15 | moduli separati, port injection e test import-boundary |

**Esito di copertura NFR:** 12/12 assegnati; i requisiti trasversali sono gate cumulativi e non verifiche una tantum.

### 5.2 US-001–US-013

| User story | Milestone di completamento | Acceptance principali |
|---|---|---|
| US-001 | M14 | AC-001, AC-003, AC-004 |
| US-002 | M03, M14 | AC-002, AC-022, AC-023 |
| US-003 | M07, M09–M10, M13 | AC-005–AC-007, AC-028, AC-036 |
| US-004 | M06–M07, M11, M13 | AC-016, AC-017, AC-029, AC-031, AC-035 |
| US-005 | M09, M11–M13 | AC-008, AC-013, AC-018, AC-024, AC-028 |
| US-006 | M06–M07, M09, M11–M13 | AC-008, AC-016–AC-019, AC-030, AC-035 |
| US-007 | M04, M13 | AC-009, AC-010 |
| US-008 | M04, M07, M12–M13 | AC-011–AC-013, AC-031 |
| US-009 | M02, M05–M08, M12–M13 | AC-014–AC-017, AC-020, AC-032, AC-033, AC-035 |
| US-010 | M09–M10, M12–M13 | AC-005, AC-006, AC-013, AC-018, AC-019, AC-024, AC-028, AC-036 |
| US-011 | M08, M12–M14 | AC-020–AC-022, AC-025, AC-033, AC-034 |
| US-012 | M04, M06, M08, M13–M14 | AC-017–AC-020, AC-025, AC-033, AC-035, AC-036 |
| US-013 | M07, M11, M14–M15 | AC-007, AC-023, AC-024, AC-027 |

**Esito di copertura user story:** 13/13 raggiungono un comportamento integrato e almeno un acceptance point.

## 6. Piano di validazione dei 36 acceptance scenario

I nomi dei test sono contratti del piano: possono essere suddivisi internamente, ma il nodo indicato deve rimanere un entry point riconoscibile per la release evidence.

| Acceptance | Milestone primaria | Punto di validazione previsto |
|---|---|---|
| AC-001 | M14 | `tests/component/test_cli_end_to_end.py::test_ac_001_single_positive_issue_only` |
| AC-002 | M03 | `tests/unit/test_config.py::test_ac_002_effective_config_precedence_and_rejection` |
| AC-003 | M13 | `tests/component/test_pipeline_multirepo.py::test_ac_003_workspace_target_split` |
| AC-004 | M09 | `tests/component/test_git_repository.py::test_ac_004_path_containment_symlink_and_top_level` |
| AC-005 | M09 | `tests/component/test_git_repository.py::test_ac_005_dirty_preflight_matrix` |
| AC-006 | M09 | `tests/component/test_git_repository.py::test_ac_006_bare_detached_and_unborn_rejected` |
| AC-007 | M07/M15 | `tests/component/test_opencode_preflight.py::test_ac_007_version_capability_and_effective_agents` più smoke M15 |
| AC-008 | M13 | `tests/component/test_single_issue_pipeline.py::test_ac_008_happy_path` |
| AC-009 | M13 | `tests/component/test_single_issue_pipeline.py::test_ac_009_rework_then_approved` |
| AC-010 | M13 | `tests/component/test_single_issue_pipeline.py::test_ac_010_review_limit_no_extra_coder` |
| AC-011 | M12 | `tests/component/test_orchestrator_attempts.py::test_ac_011_provider_recovery_same_phase_cycle` |
| AC-012 | M12 | `tests/component/test_orchestrator_attempts.py::test_ac_012_provider_exhaustion` |
| AC-013 | M12 | `tests/component/test_orchestrator_attempts.py::test_ac_013_coder_mutation_suppresses_retry` |
| AC-014 | M05 | `tests/component/test_process_runner.py::test_ac_014_timeout_kills_process_group_and_keeps_output` |
| AC-015 | M05 | `tests/component/test_process_runner.py::test_ac_015_spawn_and_non_provider_exit_are_process_error` |
| AC-016 | M06/M07 | `tests/unit/test_protocol.py::test_ac_016_terminal_only_protocol_safety` e transport fixture M07 |
| AC-017 | M06/M13 | `tests/component/test_pipeline_failures.py::test_ac_017_agent_reported_failure_is_distinct` |
| AC-018 | M09/M13 | `tests/component/test_git_checkpoints.py::test_ac_018_branch_or_head_drift_after_each_role` |
| AC-019 | M13 | `tests/component/test_pipeline_failures.py::test_ac_019_git_drift_overrides_reviewer_approval` |
| AC-020 | M13 | `tests/component/test_pipeline_failures.py::test_ac_020_all_terminal_failures_attempt_postflight` |
| AC-021 | M08/M13 | `tests/component/test_runtime_store.py::test_ac_021_retry_rework_logs_and_records_are_distinct` |
| AC-022 | M10 | `tests/component/test_runtime_location.py::test_ac_022_runtime_ignore_is_fail_closed_and_non_mutating` |
| AC-023 | M07/M11 | `tests/unit/test_agent_policy.py::test_ac_023_models_are_external_to_python_and_toml` |
| AC-024 | M11/M14 | `tests/unit/test_agent_policy.py::test_ac_024_forbidden_action_policy_and_command_inventory` |
| AC-025 | M14 | `tests/component/test_cli_end_to_end.py::test_ac_025_final_status_exit_and_run_json_consistency` |
| AC-026 | M01/M15 | esecuzione completa dei quattro comandi `QG` su macOS e Linux |
| AC-027 | M15 | `tests/integration/test_opencode_1_17_18_smoke.py::test_ac_027_disposable_compatibility_smoke` |
| AC-028 | M09/M13 | `tests/component/test_git_checkpoints.py::test_ac_028_read_only_role_mutation_and_second_m_edit` |
| AC-029 | M11/M13 | `tests/component/test_github_boundary.py::test_ac_029_identity_architect_handoff_and_failures` |
| AC-030 | M11/M13 | `tests/component/test_single_issue_pipeline.py::test_ac_030_reviewer_input_and_integral_feedback` |
| AC-031 | M07/M12 | `tests/unit/test_provider_classifier.py::test_ac_031_trusted_provider_boundary_and_precedence` |
| AC-032 | M05/M10/M13 | `tests/component/test_pipeline_failures.py::test_ac_032_unconfirmed_termination_quarantines_and_fails` |
| AC-033 | M08/M12 | `tests/component/test_runtime_store.py::test_ac_033_log_and_atomic_replace_failure` |
| AC-034 | M08/M14 | `tests/component/test_runtime_store.py::test_ac_034_posix_modes_and_private_serialization` |
| AC-035 | M06/M07 | `tests/unit/test_opencode_adapter.py::test_ac_035_transport_and_status_spoofing_fail_closed` |
| AC-036 | M09/M13 | `tests/component/test_git_checkpoints.py::test_ac_036_git_probe_failure_is_indeterminate` |

**Esito di copertura acceptance:** 36/36 hanno una milestone e un punto di validazione nominato. Nessuno smoke live sostituisce un acceptance test deterministico salvo la qualification esplicitamente richiesta da AC-027.

## 7. ADR-to-milestone map

| ADR | Milestone che lo rende eseguibile | Gate principale |
|---|---|---|
| ADR-001 — Python-owned lifecycle | M02, M04, M11–M14 | transition/import/agent-count/final-status tests |
| ADR-002 — protocol v1 fail-closed | M06–M07, M11–M13 | protocol e spoofing AC-016/017/029/031/035 |
| ADR-003 — clean baseline/fingerprint | M09, M12–M13 | Git AC-005/006/013/018/019/028/036 |
| ADR-004 — processi bounded/loop separati | M04–M05, M12–M13 | AC-010–AC-015/020/021/032/033 |
| ADR-005 — exact OpenCode/agent identity | M07, M11, M13, M15 | AC-007/023/024/027/031/035 |
| ADR-006 — lock/quarantine | M10, M13 | multiprocess lock e AC-018/032 |
| ADR-007 — GitHub identity | M03, M06, M11, M13 | AC-003/029 |
| ADR-008 — runtime/privacy | M08, M10, M14 | AC-021/022/025/033/034 |
| ADR-009 — POSIX baseline | M01, M05, M08, M10, M15 | macOS/Linux matrix e AC-014/032/034 |
| ADR-010 — threat model cooperativo | M06–M11, M14 | policy/wording e AC-024/031/034/035 |

## 8. Scope audit

### 8.1 Should-have approvati dal System Design

| Requirement | Collocazione |
|---|---|
| SH-001 — interruption | M05, M12–M13 |
| SH-002 — lock per target | M10, M13 |
| SH-003 — console summary | M14 |
| SH-004 — exit code stabili | M04, M14 |
| SH-005 — compatibility diagnosis | M07, M14 |
| SH-006 — privacy documentation | M14 |
| SH-007 — change summary | M09, M14 |

### 8.2 Could-have esplicitamente esclusi

| Requirement | Disposition |
|---|---|
| CO-001 — timeout per ruolo | Escluso; un timeout OpenCode globale v0.1 |
| CO-002 — config validate/dry-run | Escluso |
| CO-003 — console JSON | Escluso; `run.json` resta la source of truth |
| CO-004 — retention/rotation | Escluso; cleanup manuale documentato |
| CO-005 — provider signature configurabili | Escluso; allowlist versionata |
| CO-006 — jitter | Escluso; backoff deterministico |
| CO-007 — inspect run command | Escluso |
| CO-008 — resume | Escluso; ogni rilancio è una nuova run |

### 8.3 Non-obiettivi verificati

- Nessuna milestone introduce batch/range/coda, esecuzione concorrente multi-issue in una invocation o un agent `orchestrator`.
- Il lock target-scoped serializza run conformi sullo stesso checkout; non implementa batch o scheduling.
- Nessuna milestone introduce commit, branch/tag, push, merge/rebase, reset/clean/stash/checkout, PR/issue mutation, rollback o pubblicazione artifact.
- Nessuna milestone introduce worktree-per-issue, isolamento batch, orchestrazione remota, servizio/daemon, database/coda, API/webhook, UI/TUI o plugin system.
- Nessuna milestone introduce selezione modello, provider credential management o provider API in Python.
- Nessuna milestone promette correttezza semantica dell'AI o sandbox contro un processo deliberatamente ostile.
- Windows native, WSL su filesystem Windows e filesystem remoti restano fuori dalla matrice v0.1.

**Esito scope:** nessuna feature fuori scope v0.1 è entrata nel piano.

## 9. Gap e release evidence ancora da produrre

Non esistono requisiti PRD/System Design/ADR impossibili da collocare senza reinterpretazione: **gap di placement = nessuno**.

Restano tre gate di evidenza già previsti dalle fonti canoniche, non nuove decisioni:

1. **OpenCode qualification.** M07 può produrre soltanto un adapter candidato. Fixture complete e smoke M15 devono dimostrare transport e agent identity per `1.17.18`; in caso contrario la versione resta non supportata, la release è bloccata e ADR-005 va riesaminato senza fallback.
2. **Fingerprint scalability.** M09 deve misurare `git-state-v1` su repository grandi. Se supera il timeout utility, si documenta/configura un valore maggiore entro i range approvati; non si degrada al solo porcelain.
3. **Platform matrix.** M15 deve eseguire quality/acceptance su macOS e Linux POSIX. Un PASS su una sola piattaforma non qualifica entrambe.

## 10. Checklist di chiusura v0.1

- [ ] M01–M15 completate nell'ordine consentito dalle dipendenze e con `QG` cumulativo verde.
- [ ] FR-001–FR-052: 52/52 con test e artifact di evidenza.
- [ ] NFR-001–NFR-012: 12/12 verificati, inclusi typing, boundedness, privacy e dependency graph.
- [ ] US-001–US-013: 13/13 completate.
- [ ] AC-001–AC-036: 36/36 PASS nei punti nominati in §6.
- [ ] SH-001–SH-007 implementati come approvato dal System Design.
- [ ] CO-001–CO-008 e non-obiettivi assenti dal diff di release.
- [ ] Nessun comando Python mutativo Git/GitHub e nessun flag OpenCode vietato nell'inventario.
- [ ] Nessun model ID o credential nel package/TOML/run JSON.
- [ ] Modifiche di test e smoke restano non committate dal programma e nessuna publication automatica è avvenuta.
- [ ] OpenCode `1.17.18` dichiarato supportato soltanto dopo fixture provenance e smoke AC-027.
- [ ] `docs/compatibility.md`, `docs/security-and-privacy.md`, `docs/recovery.md` e README riflettono esclusivamente comportamento provato.
- [ ] Review finale conferma che nessun contratto congelato del System Design §23.2 è stato modificato senza aggiornare fonte/ADR/traceability.

Il completamento della checklist autorizza una decisione di release; non autorizza commit, push, merge o altra pubblicazione automatica.
