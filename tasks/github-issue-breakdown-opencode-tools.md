# GitHub Issue Breakdown: OpenCode-Tools v0.1

| Campo | Valore |
|---|---|
| Stato | Breakdown operativo canonico subordinato alle fonti di prodotto e design |
| Release target | v0.1 |
| Fonte prodotto | [`tasks/prd-opencode-tools.md`](./prd-opencode-tools.md) |
| Design vincolante | [`tasks/system-design-opencode-tools.md`](./system-design-opencode-tools.md) e ADR accettati in [`docs/adr/`](../docs/adr/) |
| Sequenza vincolante | [`tasks/implementation-plan-opencode-tools.md`](./implementation-plan-opencode-tools.md) |
| Scopo | Issue locali copiabili; nessuna issue remota, implementazione o modifica delle fonti canoniche |

Questo documento trasforma M01–M15 in unità piccole e verificabili. Non modifica requisiti, architettura, dipendenze o gate. In caso di divergenza prevalgono PRD, System Design, ADR e Implementation Plan, in quest'ordine di responsabilità già definito dalle fonti.

## Catalogo completo

Le dipendenze espresse come milestone (`Mxx`) significano che la Definition of Done dell'intera milestone deve essere soddisfatta. Le dipendenze fra issue della stessa milestone rendono esplicita soltanto la sequenza necessaria fra i work package già previsti.

| Milestone | Issue | Titolo | Dipendenze | Stato bloccante |
|---|---|---|---|---|
| M01 | M01-01 | Configurare package Python 3.13+ e metadata | Nessuna | No |
| M01 | M01-02 | Configurare quality tooling e import-boundary scaffold | M01-01 | No |
| M01 | M01-03 | Predisporre artifact locali ignorati e setup README | M01-01 | No |
| M02 | M02-01 | Definire enum e value object immutabili del dominio | M01 | No |
| M02 | M02-02 | Implementare error model typed e conversione ErrorRecord | M02-01 | No |
| M02 | M02-03 | Definire le porte applicative sostituibili | M02-01 | No |
| M02 | M02-04 | Imporre le regole di import tra dominio, low-level e orchestrator | M02-01, M02-02, M02-03 | No |
| M03 | M03-01 | Validare RunRequest, workspace e target | M02 | No |
| M03 | M03-02 | Implementare lookup e precedence della configurazione v1 | M02 | No |
| M03 | M03-03 | Implementare default, range e validazione strict della config | M03-02 | No |
| M03 | M03-04 | Validare runtime root e override GitHub target-specifici | M03-01, M03-02, M03-03 | No |
| M04 | M04-01 | Implementare state machine e review-cycle transitions | M02, M03 | No |
| M04 | M04-02 | Implementare provider retry policy e bound teorico | M02, M03 | No |
| M04 | M04-03 | Implementare precedence, final gate e mapping terminale | M04-01, M04-02 | No |
| M05 | M05-01 | Implementare spawn POSIX e ProcessResult | M02, M03 | No |
| M05 | M05-02 | Implementare drain concorrente e bounded di stdout/stderr | M05-01 | No |
| M05 | M05-03 | Implementare timeout, process-group termination e cancellation | M05-01, M05-02 | No |
| M05 | M05-04 | Gestire sink fault, spawn failure e non-zero exit | M05-01, M05-02, M05-03 | No |
| M06 | M06-01 | Validare marker terminale, ruolo/status e body | M02 | No |
| M06 | M06-02 | Validare ISSUE_REF_JSON v1 e identity canonica | M06-01 | No |
| M07 | M07-01 | Costruire fixture offline OpenCode 1.17.18 con provenance | M03, M05, M06 | No; adapter ancora candidato |
| M07 | M07-02 | Implementare exact-version e capability/control-plane preflight | M07-01 | No; adapter ancora candidato |
| M07 | M07-03 | Implementare command builder OpenCode e deny-list dei flag | M07-02 | No; adapter ancora candidato |
| M07 | M07-04 | Implementare decoder e transport NDJSON 1.17.18 | M07-01, M05, M06 | No; adapter ancora candidato |
| M07 | M07-05 | Verificare effective agent identity tramite export sanitizzato | M07-02, M07-04 | No; qualification rinviata a M15-03 |
| M07 | M07-06 | Implementare classifier provider versionato | M07-01, M07-04 | No; adapter ancora candidato |
| M08 | M08-01 | Implementare run ID, directory layout e naming privato | M02, M03, M05 | No |
| M08 | M08-02 | Implementare schema run.json v1 e serializzazione deterministica | M02, M03, M05 | No |
| M08 | M08-03 | Implementare persistenza atomica e fault handling | M08-01, M08-02 | No |
| M08 | M08-04 | Implementare attempt log append-only e sink | M08-01, M05 | No |
| M09 | M09-01 | Validare top-level Git, branch/HEAD e baseline clean | M02, M03, M05 | No |
| M09 | M09-02 | Implementare fingerprint git-state-v1 content-sensitive | M09-01 | No |
| M09 | M09-03 | Gestire stabilità di campionamento e stato INDETERMINATE | M09-02 | No |
| M09 | M09-04 | Implementare checkpoint before/after e role mutation policy | M09-02, M09-03 | No |
| M09 | M09-05 | Implementare change inventory e postflight preservation | M09-01, M09-04 | No |
| M09 | M09-06 | [GATE BLOCCANTE M09] Qualificare la scalabilità di git-state-v1 | M09-02, M09-03 | Sì: blocca M09 e i suoi dipendenti, non M01–M08 |
| M10 | M10-01 | Validare runtime root e platform/filesystem baseline | M03, M05, M08, M09 | No |
| M10 | M10-02 | Implementare coordination directory e target-scoped flock | M10-01 | No |
| M10 | M10-03 | Implementare quarantine persistente per termination incerta | M10-02 | No |
| M11 | M11-01 | Risolvere repository identity target-specifica | M03, M06, M07, M09 | No |
| M11 | M11-02 | Implementare gh preflight e IssueLocator-only boundary | M11-01 | No |
| M11 | M11-03 | Costruire prompt canonicali per architect, coder e reviewer | M11-01, M06 | No |
| M11 | M11-04 | Definire i tre agenti e verificarne la permission matrix | M07, M11-03 | No |
| M12 | M12-01 | Implementare skeleton port-only della logical invocation | M04–M11 | No |
| M12 | M12-02 | Integrare checkpoint, control-plane check, process e protocol precedence | M12-01 | No |
| M12 | M12-03 | Persistire attempt distinti e gestire logging/cancellation | M12-01, M12-02 | No |
| M12 | M12-04 | Integrare provider retry guard ed exhaustion | M12-02, M12-03 | No |
| M13 | M13-01 | Comporre bootstrap, preflight e lease lifetime | M12 | No |
| M13 | M13-02 | Comporre happy path architect → coder → reviewer | M13-01 | No |
| M13 | M13-03 | Comporre review/rework loop e provider retry per ciclo | M13-02 | No |
| M13 | M13-04 | Convergere terminal path in postflight e finalizzazione | M13-01, M13-02, M13-03 | No |
| M13 | M13-05 | Qualificare failure matrix e multi-repository con fake | M13-01, M13-02, M13-03, M13-04 | No |
| M14 | M14-01 | Implementare comando e parser CLI single-issue | M01–M13 | No |
| M14 | M14-02 | Implementare composition root e port wiring | M14-01 | No |
| M14 | M14-03 | Implementare final rendering, exit code e summary | M14-02 | No |
| M14 | M14-04 | Completare documentazione operatore conforme al comportamento provato | M14-01, M14-02, M14-03 | No |
| M15 | M15-01 | [RELEASE BLOCKER M15] Chiudere la matrice deterministica AC-001–AC-036 | M14 | Sì: blocca la release |
| M15 | M15-02 | [RELEASE BLOCKER M15] Eseguire la matrice macOS/Linux POSIX | M14 | Sì: blocca la release, non milestone precedenti |
| M15 | M15-03 | [RELEASE BLOCKER M15] Qualificare OpenCode 1.17.18 | M14 | Sì: blocca support claim e release, non milestone precedenti |

## DAG delle milestone

```text
M01: —
M02: M01
M03: M02
M04: M02, M03
M05: M02, M03
M06: M02
M07: M03, M05, M06
M08: M02, M03, M05
M09: M02, M03, M05
M10: M03, M05, M08, M09
M11: M03, M06, M07, M09
M12: M04–M11
M13: M12
M14: M01–M13
M15: M14
```

Catena critica: `M01 → M02 → M03 → M05 → M07 → M11 → M12 → M13 → M14 → M15`. M04, M06, M08, M09 e M10 possono avanzare appena verdi i rispettivi prerequisiti. Il gate M09-06 entra soltanto in M09; le issue bloccanti M15-01–M15-03 entrano soltanto nella qualification finale.

## Quality gate comune

Ogni issue ripete il gate cumulativo esatto fissato dal piano. Il comando mirato della milestone va eseguito prima del gate.

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

## Comandi mirati di chiusura milestone

Ogni issue usa un comando mirato ai file che introduce, così da restare implementabile e revisionabile in modo indipendente. Dopo che tutte le issue di una milestone sono chiuse, si esegue anche il comando mirato completo fissato dall'Implementation Plan e poi il `QG` comune.

| Milestone | Comando mirato completo di chiusura |
|---|---|
| M01 | <code>PYTHONPATH=src python3.13 -m pytest tests/unit/test_package.py tests/unit/test_import_boundaries.py</code> |
| M02 | <code>python3.13 -m pytest tests/unit/test_domain.py tests/unit/test_errors.py tests/unit/test_ports.py tests/unit/test_import_boundaries.py</code> |
| M03 | <code>python3.13 -m pytest tests/unit/test_config.py tests/unit/test_paths.py</code> |
| M04 | <code>python3.13 -m pytest tests/unit/test_state_machine.py tests/unit/test_retry.py tests/unit/test_final_gate.py</code> |
| M05 | <code>python3.13 -m pytest tests/unit/test_process_results.py tests/component/test_process_runner.py</code> |
| M06 | <code>python3.13 -m pytest tests/unit/test_protocol.py</code> |
| M07 | <code>python3.13 -m pytest tests/unit/test_opencode_adapter.py tests/unit/test_provider_classifier.py tests/component/test_opencode_preflight.py</code> |
| M08 | <code>python3.13 -m pytest tests/unit/test_run_schema.py tests/unit/test_runlog_naming.py tests/component/test_runtime_store.py</code> |
| M09 | <code>python3.13 -m pytest tests/unit/test_git_fingerprint.py tests/unit/test_git_commands.py tests/component/test_git_repository.py tests/component/test_git_checkpoints.py</code> |
| M10 | <code>python3.13 -m pytest tests/unit/test_runtime_bootstrap.py tests/component/test_locking.py tests/component/test_runtime_location.py</code> |
| M11 | <code>python3.13 -m pytest tests/unit/test_github_identity.py tests/unit/test_prompting.py tests/unit/test_agent_policy.py tests/component/test_github_boundary.py</code> |
| M12 | <code>python3.13 -m pytest tests/unit/test_attempt_precedence.py tests/component/test_orchestrator_attempts.py</code> |
| M13 | <code>python3.13 -m pytest tests/component/test_single_issue_pipeline.py tests/component/test_pipeline_failures.py tests/component/test_pipeline_multirepo.py</code> |
| M14 | <code>python3.13 -m pytest tests/unit/test_cli.py tests/component/test_cli_end_to_end.py</code><br><code>PYTHONPATH=src python3.13 -m opencode_tools --help</code> |
| M15 | Intero `QG`; poi, separatamente, <code>python3.13 -m pytest tests/integration/test_opencode_1_17_18_smoke.py -m live</code> |

## Issue copiabili

### M01-01

**Titolo:** Configurare package Python 3.13+ e metadata

**Contesto:** M01 deve rendere il repository un package importabile con Python 3.13+, `src` layout e nessuna dipendenza Python runtime di terze parti.

**Obiettivo:** Congelare il manifest minimo, i metadata e la versione pubblica del package.

**Scope:** Configurare `requires-python = ">=3.13"`; dichiarare solo pytest, Ruff e mypy come dipendenze di sviluppo; mantenere vuote le dipendenze runtime; esporre `opencode_tools.__version__`; verificare import, versione e manifest.

**Fuori scope:** CLI, lifecycle, adapter, dipendenze runtime, installazione di strumenti esterni e qualunque feature Could-Have.

**File/componenti previsti:** `pyproject.toml`; `src/opencode_tools/__init__.py`; `tests/unit/test_package.py`.

**Requisiti PRD coperti:** NFR-001, NFR-002, NFR-003; punto di verifica AC-026.

**ADR applicabili:** ADR-001; ADR-009.

**Dipendenze:** Nessuna.

**Stato bloccante:** Non è un gate dedicato; la sua DoD è prerequisito di M02 e delle milestone successive.

**Acceptance criteria:**

- Il package è importabile da `src` con Python 3.13+.
- `opencode_tools.__version__` è disponibile.
- Il manifest non dichiara dipendenze runtime e limita i tool di sviluppo a quelli previsti.

**Test richiesti:** Test unitari di import/versione, requisito Python e audit del manifest.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
PYTHONPATH=src python3.13 -m pytest tests/unit/test_package.py
```

**Definition of Done:** Manifest, import e versione sono verdi; il package resta privo di package runtime e supera comando mirato e quality gate.

### M01-02

**Titolo:** Configurare quality tooling e import-boundary scaffold

**Contesto:** Il piano rende pytest, Ruff, Ruff format e mypy strict un gate cumulativo dalla prima milestone.

**Obiettivo:** Rendere eseguibili e riproducibili i controlli quality e predisporre il controllo del grafo degli import.

**Scope:** Configurare pytest, Ruff e mypy strict; creare lo scaffold `test_import_boundaries.py`; documentare i quattro comandi esatti nel README.

**Fuori scope:** Regole architetturali non ancora definite da M02, CI non prevista dalle fonti, dipendenze runtime e rilassamento dei gate.

**File/componenti previsti:** `pyproject.toml`; `README.md`; `tests/unit/test_import_boundaries.py`.

**Requisiti PRD coperti:** NFR-003, NFR-004, NFR-012; AC-026.

**ADR applicabili:** ADR-001; ADR-009.

**Dipendenze:** M01-01.

**Stato bloccante:** Non è un gate dedicato; il quality gate deve restare verde per chiudere ogni milestone.

**Acceptance criteria:**

- I quattro comandi canonici sono configurati e documentati senza varianti.
- Lo scaffold import-boundary è eseguibile e verde.
- Mypy opera in modalità strict su `src` e `tests`.

**Test richiesti:** Test dello scaffold import-boundary ed esecuzione integrale dei quattro quality command.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
PYTHONPATH=src python3.13 -m pytest tests/unit/test_package.py tests/unit/test_import_boundaries.py
```

**Definition of Done:** Configurazione e documentazione coincidono; tutti i comandi sono eseguibili e lo scaffold import-boundary passa.

### M01-03

**Titolo:** Predisporre artifact locali ignorati e setup README

**Contesto:** Gli artifact v0.1 vivono sotto `.opencode-tools/`, ma il programma non deve modificare automaticamente le ignore rule.

**Obiettivo:** Preparare il repository per gli artifact locali e documentare il setup iniziale senza attribuire mutation al programma.

**Scope:** Aggiungere `.opencode-tools/` alle ignore rule del repository; documentare setup e quality command iniziali.

**Fuori scope:** Creazione runtime, verifica ignore fail-closed, mode/ownership, lock, retention o cleanup automatico; questi confini appartengono a M10/M14.

**File/componenti previsti:** `.gitignore`; `README.md`; test package/import-boundary di M01.

**Requisiti PRD coperti:** Base per FR-015; NFR-004; punto di verifica cumulativo AC-026. AC-022 resta a M10.

**ADR applicabili:** ADR-001; ADR-009.

**Dipendenze:** M01-01.

**Stato bloccante:** No; FR-015 e AC-022 saranno completati da M10.

**Acceptance criteria:**

- `.opencode-tools/` è ignorata nel repository.
- La documentazione non promette che Python aggiunga o corregga ignore rule.
- Il setup riporta i quality command canonici.

**Test richiesti:** Audit manifest/import e verifica repository-level della regola ignorata prevista dalla baseline.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
git check-ignore --quiet --no-index .opencode-tools/.probe
```

**Definition of Done:** Ignore rule e README sono coerenti con il confine non mutativo; comando mirato e quality gate passano.

### M02-01

**Titolo:** Definire enum e value object immutabili del dominio

**Contesto:** Phase, outcome, status agent/review/finale, Git safety e persistenza devono restare dimensioni distinte e deterministiche.

**Obiettivo:** Congelare i contratti typed e immutabili condivisi da policy e adapter.

**Scope:** Definire `StrEnum` canonici, inclusi `PREFLIGHT_ERROR` e `INTERRUPTED`; value object frozen/slotted per workspace, target, repository/issue identity, process, agent, Git, attempt, error, run e result; campi nullable solo dove previsto; tuple/copie difensive e conversione verso primitive JSON.

**Fuori scope:** I/O, transizioni, retry, parsing, persistenza e implementazioni concrete delle porte.

**File/componenti previsti:** `src/opencode_tools/domain.py`; `tests/unit/test_domain.py`.

**Requisiti PRD coperti:** FR-010, FR-024, FR-027, FR-034, FR-045; NFR-003, NFR-007, NFR-011, NFR-012.

**ADR applicabili:** ADR-001; ADR-002; ADR-004; ADR-008.

**Dipendenze:** M01.

**Stato bloccante:** Non è un gate dedicato; è il contratto base di M02 e dei moduli successivi.

**Acceptance criteria:**

- Tutti gli stati canonici sono rappresentabili senza enum omnicomprensivi.
- I record sono immutabili, slotted e difensivi rispetto alle collezioni.
- Le dimensioni nullable e il round-trip verso primitive JSON rispettano lo schema previsto.

**Test richiesti:** Valori enum, invarianti costruttive, immutabilità, copie difensive, nullable e round-trip JSON.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_domain.py
```

**Definition of Done:** I contratti di dominio sono completi, typed, immutabili, privi di I/O e superano comando mirato e quality gate.

### M02-02

**Titolo:** Implementare error model typed e conversione ErrorRecord

**Contesto:** Le cause devono restare distinguibili e concorrenti, senza essere appiattite in un singolo stato globale.

**Obiettivo:** Modellare eccezioni interne tipizzate e convertirle deterministicamente in record serializzabili.

**Scope:** Definire categorie e dati d'errore typed; conversione in `ErrorRecord` con phase e riferimenti applicabili; cause multiple preservate; messaggi sanitizzati.

**Fuori scope:** Decisioni di retry, precedence o finalizzazione; persistenza dei record; raw stderr, environment, credential o traceback non necessari.

**File/componenti previsti:** `src/opencode_tools/errors.py`; aggiornamenti mirati a `src/opencode_tools/domain.py`; `tests/unit/test_errors.py`.

**Requisiti PRD coperti:** FR-034, FR-045; NFR-008, NFR-011.

**ADR applicabili:** ADR-002; ADR-004; ADR-008.

**Dipendenze:** M02-01.

**Stato bloccante:** Non è un gate dedicato; abilita policy, adapter e persistenza successive.

**Acceptance criteria:**

- Ogni categoria canonica ha una rappresentazione typed.
- Le cause concorrenti restano separate e ordinate.
- La conversione non decide retry o finalizzazione e non introduce dati sensibili.

**Test richiesti:** Mapping eccezione-record, cause multiple, nullable e sanitizzazione.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_errors.py
```

**Definition of Done:** Error model completo e lossless nelle dimensioni canoniche, senza lifecycle implicito; comando mirato e quality gate verdi.

### M02-03

**Titolo:** Definire le porte applicative sostituibili

**Contesto:** Processi, tempo, sleep, log, agent, Git, GitHub, store e lease devono poter essere sostituiti da fake deterministici.

**Obiettivo:** Congelare i `Protocol` usati dagli strati applicativi senza introdurre implementazioni concrete.

**Scope:** Porte per process runner, clock, sleeper, attempt sink, agent runner, Git safety, issue resolver, run store e lease factory; firme basate sui tipi di dominio.

**Fuori scope:** Adapter concreti, service locator, singleton, I/O o lifecycle nelle porte.

**File/componenti previsti:** `src/opencode_tools/ports.py`; `tests/unit/test_ports.py`.

**Requisiti PRD coperti:** NFR-006, NFR-011, NFR-012; supporto trasversale agli acceptance test deterministici.

**ADR applicabili:** ADR-001; ADR-004; ADR-008.

**Dipendenze:** M02-01.

**Stato bloccante:** Non è un gate dedicato; è prerequisito tecnico per M04–M13.

**Acceptance criteria:**

- Ogni boundary previsto dispone di un `Protocol` sostituibile.
- Le firme usano tipi di dominio e non importano adapter concreti.
- Fake minimali soddisfano le porte e passano mypy strict.

**Test richiesti:** Fake/recording implementation per ogni porta e verifica delle firme essenziali.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_ports.py
```

**Definition of Done:** Tutti gli adapter futuri sono iniettabili e testabili senza sistemi esterni; comando mirato e quality gate passano.

### M02-04

**Titolo:** Imporre le regole di import tra dominio, low-level e orchestrator

**Contesto:** Il design vieta dipendenze inverse: il dominio non importa moduli applicativi e i moduli low-level non dipendono dall'orchestrator.

**Obiettivo:** Rendere il grafo degli import un contratto eseguibile.

**Scope:** Completare i test import-boundary; vietare import applicativi dal dominio, import dell'orchestrator dai low-level e conoscenza OpenCode nel process layer; coprire casi validi e violazioni.

**Fuori scope:** Implementare orchestrator o adapter, introdurre framework DI o reinterpretare la struttura modulare.

**File/componenti previsti:** `tests/unit/test_import_boundaries.py`; verifiche su `domain.py`, `errors.py`, `ports.py`.

**Requisiti PRD coperti:** NFR-003, NFR-006, NFR-012.

**ADR applicabili:** ADR-001.

**Dipendenze:** M02-01, M02-02, M02-03.

**Stato bloccante:** Non è un gate dedicato; chiude M02 e protegge tutte le milestone successive.

**Acceptance criteria:**

- Il dominio non importa moduli applicativi.
- Nessun modulo low-level importa `orchestrator.py`.
- Le fixture di violazione falliscono e il grafo valido resta verde.

**Test richiesti:** Casi positivi e negativi del grafo d'import.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_domain.py tests/unit/test_errors.py tests/unit/test_ports.py tests/unit/test_import_boundaries.py
```

**Definition of Done:** Confini architetturali codificati in test, M02 interamente verde e nessuna dipendenza inversa.

### M03-01

**Titolo:** Validare RunRequest, workspace e target

**Contesto:** Ogni invocation accetta una sola issue; workspace OpenCode e target Git sono path distinti e il target deve restare contenuto nel workspace.

**Obiettivo:** Produrre un `RunRequest` valido prima di agent o artifact.

**Scope:** Issue come intero positivo; workspace `resolve(strict=True)`; target solo `.` o relativo; canonicalizzazione e containment dopo symlink resolution; rifiuto di target assoluti, traversal ed escape.

**Fuori scope:** Prova Git top-level, baseline clean, runtime ownership/ignore e creazione artifact, rinviati a M09/M10.

**File/componenti previsti:** `src/opencode_tools/config.py`; aggiornamenti `domain.py`/`errors.py`; `tests/unit/test_paths.py`.

**Requisiti PRD coperti:** FR-001, FR-003, parte path di FR-004; NFR-008; AC-001 e AC-004 parziali.

**ADR applicabili:** ADR-003; ADR-008; ADR-010.

**Dipendenze:** M02.

**Stato bloccante:** Non è un gate dedicato; input invalido deve fermarsi prima di agent/artifact.

**Acceptance criteria:**

- Issue nulla, negativa, bool o non scalare è rifiutata.
- Workspace inesistente e target assoluto/traversal/symlink escape sono rifiutati.
- Input valido produce path canonici distinti e contained.

**Test richiesti:** Matrice issue/path, target `.`, relativo, traversal e symlink escape; fixture filesystem temporanei.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_paths.py
```

**Definition of Done:** Ogni request valida produce path canonici stabili; ogni input invalido fallisce prima di I/O agent/artifact; gate verdi.

### M03-02

**Titolo:** Implementare lookup e precedence della configurazione v1

**Contesto:** La configurazione effettiva proviene da file esplicito, file convenzionale nel workspace o default built-in, con schema TOML chiuso `version = 1`.

**Obiettivo:** Rendere deterministici discovery, precedence e source della configurazione.

**Scope:** Risolvere `--config` rispetto al cwd di invocazione; lookup `<workspace>/opencode-tools.toml`; fallback built-in; parsing `tomllib`; source `explicit`, `conventional` o `defaults`; version/schema chiusi.

**Fuori scope:** Override environment, model/provider ID, override CLI dei singoli lifecycle parameter e comando config validate/dry-run.

**File/componenti previsti:** `src/opencode_tools/config.py`; `tests/unit/test_config.py`; `tests/fixtures/config/`.

**Requisiti PRD coperti:** FR-006, FR-007, FR-008, FR-009; NFR-008; AC-002, AC-023.

**ADR applicabili:** ADR-007; ADR-008; ADR-010.

**Dipendenze:** M02.

**Stato bloccante:** Non è un gate dedicato; config invalida deve fermarsi pre-init.

**Acceptance criteria:**

- Precedence e source sono esatte e riproducibili.
- File mancante esplicito, version mismatch e chiavi sconosciute/duplicate falliscono.
- Nessun model/provider ID o override non previsto entra nello schema.

**Test richiesti:** Default, esplicito, convenzionale, precedence, file mancante, version mismatch, schema chiuso e fixture golden/invalidi.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_config.py
```

**Definition of Done:** A parità di input la source e l'`AppConfig` sono identici; input non riconosciuti falliscono pre-init; gate verdi.

### M03-03

**Titolo:** Implementare default, range e validazione strict della config

**Contesto:** Timeout, grace, review cycle e provider retry hanno default e range congelati; bool e numeri non finiti non sono accettabili.

**Obiettivo:** Applicare tutti i vincoli scalar e cross-field dello schema v1.

**Scope:** Validare i valori canonici di `execution.*`, `provider_retry.*` e `runtime.root`; rifiutare bool come numeri, NaN/infinito, range invalidi e `max_delay < initial_delay`; produrre l'`AppConfig` typed.

**Fuori scope:** Timeout per ruolo, jitter, signature provider configurabili e qualunque default diverso dalle fonti.

**File/componenti previsti:** `src/opencode_tools/config.py`; aggiornamenti `domain.py`/`errors.py`; `tests/unit/test_config.py`; fixture config.

**Requisiti PRD coperti:** FR-006–FR-009; NFR-005, NFR-008; AC-002, AC-014, AC-023.

**ADR applicabili:** ADR-004; ADR-008; ADR-010.

**Dipendenze:** M03-02.

**Stato bloccante:** Non è un gate dedicato; ogni valore invalido blocca pre-init.

**Acceptance criteria:**

- Tutti i default e range coincidono col piano.
- Type check e cross-field rifiutano ogni caso limite invalido, inclusi bool e non-finite.
- La configurazione effettiva non contiene model/provider ID.

**Test richiesti:** Boundary test di ogni range, cross-field, bool/non-finite e default exact.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_config.py
```

**Definition of Done:** Default e validazioni sono totali, deterministicamente testati e coerenti con lo schema v1; gate verdi.

### M03-04

**Titolo:** Validare runtime root e override GitHub target-specifici

**Contesto:** Il runtime root può essere relativo al workspace o assoluto, mentre la repository GitHub può essere risolta con override target-specifici chiusi.

**Obiettivo:** Completare i confini path/config senza eseguire probe Git o GitHub.

**Scope:** Canonicalizzare `runtime.root`, rifiutarlo sotto metadata Git e richiederlo non vuoto; validare `github.targets` senza duplicati e con almeno `remote` o `repository`; serializzare effective config sanitizzata con source.

**Fuori scope:** Ignore/owner/mode runtime di M10, risoluzione repository/gh di M11, creazione directory e qualunque network I/O.

**File/componenti previsti:** `src/opencode_tools/config.py`; `domain.py`; `errors.py`; `tests/unit/test_config.py`; `tests/unit/test_paths.py`; fixture config.

**Requisiti PRD coperti:** FR-003, FR-004 lato path, FR-006–FR-008; NFR-008; AC-002, AC-004, AC-023 parziali.

**ADR applicabili:** ADR-003; ADR-007; ADR-008; ADR-010.

**Dipendenze:** M03-01, M03-02, M03-03.

**Stato bloccante:** Non è un gate dedicato; path o override ambigui bloccano pre-init.

**Acceptance criteria:**

- Runtime root valido è canonico e mai sotto metadata Git.
- Override duplicati o privi di remote/repository falliscono.
- La config sanitizzata espone source e valori effettivi senza secret.

**Test richiesti:** Fixture cwd/workspace/runtime; root relativo/assoluto/metadata; override validi, duplicati e incompleti.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_config.py tests/unit/test_paths.py
```

**Definition of Done:** Path e override producono value object stabili o errori pre-init fail-closed; nessun I/O agent/Git/GitHub; gate verdi.

### M04-01

**Titolo:** Implementare state machine e review-cycle transitions

**Contesto:** Il lifecycle canonico è `PREFLIGHT → ARCHITECT → CODER(n) → REVIEWER(n) → POSTFLIGHT → FINALIZATION → FINISHED` e resta posseduto da Python.

**Obiettivo:** Rendere le transizioni e il review cycle funzioni pure ed esaustivamente testabili.

**Scope:** Eventi/azioni tipizzati per READY, COMPLETED, APPROVED, CHANGES_REQUIRED e terminal outcome; cycle da 1; incremento solo dopo changes required; exhaustion senza coder ulteriore; transizioni invalide rifiutate.

**Fuori scope:** I/O, processi, Git, sleep, parser raw, orchestrazione integrata e quarto agent orchestrator.

**File/componenti previsti:** `src/opencode_tools/state_machine.py`; aggiornamenti `domain.py`; `tests/unit/test_state_machine.py`.

**Requisiti PRD coperti:** FR-016, FR-024–FR-026, FR-030, FR-047–FR-049; NFR-006, NFR-011, NFR-012; SH-004; AC-009, AC-010, AC-025.

**ADR applicabili:** ADR-001; ADR-002; ADR-004.

**Dipendenze:** M02, M03.

**Stato bloccante:** Non è un gate dedicato; il contratto è prerequisito di M12.

**Acceptance criteria:**

- Tutte le transizioni valide producono l'azione prevista e quelle invalide falliscono.
- Il cycle inizia da 1 e cresce solo dopo `CHANGES_REQUIRED`.
- L'exhaustion non invoca un coder aggiuntivo.

**Test richiesti:** Tabella esaustiva, cycle 1/N, transition valide/invalide e trace deterministico.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_state_machine.py
```

**Definition of Done:** State machine pura e totale; trace identico a parità di eventi; comando mirato e quality gate verdi.

### M04-02

**Titolo:** Implementare provider retry policy e bound teorico

**Contesto:** Provider attempt e review cycle sono contatori e loop separati; il backoff è capped e senza jitter.

**Obiettivo:** Rendere retry, delay e bound massimo decisioni pure.

**Scope:** Attempt da 1 per logical invocation; formula capped; guard canonici; exhaustion; delay planned/actual; funzione per il bound teorico configurato; nessun jitter.

**Fuori scope:** Sleep reale, classifier raw OpenCode, retry non-provider, timeout per ruolo e modifica del review cycle.

**File/componenti previsti:** `src/opencode_tools/retry.py`; aggiornamenti `domain.py`; `tests/unit/test_retry.py`.

**Requisiti PRD coperti:** FR-027–FR-030; NFR-005–NFR-007, NFR-011; AC-011, AC-012, AC-021.

**ADR applicabili:** ADR-004.

**Dipendenze:** M02, M03.

**Stato bloccante:** Non è un gate dedicato; la policy è prerequisito di M12.

**Acceptance criteria:**

- Delay e cap coincidono esattamente con la formula canonica.
- Retry avviene solo quando tutti i guard sono veri.
- Retry non modifica cycle e rework non riusa attempt precedenti.

**Test richiesti:** Delay esatti/capped, attempt 1/N, guard matrix, exhaustion, counter indipendenti e bound massimo.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_retry.py
```

**Definition of Done:** Retry policy deterministica, bounded e priva di I/O; nessuna contaminazione col review cycle; gate verdi.

### M04-03

**Titolo:** Implementare precedence, final gate e mapping terminale

**Contesto:** Esiti tecnici, Git safety, persistenza e cause concorrenti restano ortogonali; reviewer approval da sola non basta.

**Obiettivo:** Rendere totalizzanti precedence, final approval gate e mapping outcome/final status/exit family.

**Scope:** Precedence attempt e pipeline; cause multiple preservate; approval soltanto con reviewer approval, postflight SAFE, branch/HEAD invariati e persistence OK; mapping totale; final gate unitario.

**Fuori scope:** Rendering CLI, esecuzione postflight, persistenza o parsing; nessun collasso delle cause in un solo enum.

**File/componenti previsti:** `src/opencode_tools/state_machine.py`; `src/opencode_tools/retry.py`; `domain.py`; `tests/unit/test_attempt_precedence.py` futuro; `tests/unit/test_final_gate.py`.

**Requisiti PRD coperti:** FR-034, FR-040, FR-047–FR-049; NFR-011, NFR-012; SH-004; AC-012–AC-020, AC-025 a livello policy.

**ADR applicabili:** ADR-001; ADR-002; ADR-004.

**Dipendenze:** M04-01, M04-02.

**Stato bloccante:** Non è un gate dedicato; nessun approval può essere emesso senza questa policy verde.

**Acceptance criteria:**

- Ogni combinazione rilevante ha precedence e mapping totali.
- Cause concorrenti restano registrate.
- Drift, postflight unsafe o persistence failure impediscono approval anche dopo reviewer approval.

**Test richiesti:** Cause concorrenti, gate final, mapping completo e precedence per attempt/pipeline.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_state_machine.py tests/unit/test_retry.py tests/unit/test_final_gate.py
```

**Definition of Done:** Final gate e mapping sono puri, totali e fail-closed; reviewer approval non aggira Git/persistence; gate verdi.

### M05-01

**Titolo:** Implementare spawn POSIX e ProcessResult

**Contesto:** Ogni child locale deve essere eseguito tramite un boundary generico, bounded e indipendente da OpenCode, Git e protocollo agent.

**Obiettivo:** Implementare lo spawn sicuro e il risultato tecnico tipizzato del processo.

**Scope:** `subprocess.Popen`; argv tuple con `shell=False`; executable assoluto; cwd per child senza `os.chdir`; stdin separato; environment ereditato ma non enumerato o persistito; timestamp UTC, `monotonic_ns`, digest/byte count e return code nullable.

**Fuori scope:** Parsing agent/provider, lifecycle, Git, prompt in argv/environment, supporto Windows.

**File/componenti previsti:** `src/opencode_tools/process.py`; aggiornamenti a `ports.py`, `domain.py`, `errors.py`; `tests/unit/test_process_results.py`; `tests/component/test_process_runner.py`; helper component locali.

**Requisiti PRD coperti:** FR-032–FR-034 lato process boundary; NFR-002, NFR-005–NFR-007, NFR-010; AC-014, AC-015, AC-034 parziali.

**ADR applicabili:** ADR-004; ADR-008; ADR-009; ADR-010.

**Dipendenze:** M02, M03.

**Stato bloccante:** Non è un gate dedicato; è la base di M05 e prerequisito di M07–M10.

**Acceptance criteria:**

- Nessun child usa una shell o modifica il cwd globale.
- Stdin e prompt non entrano in argv o nell'environment.
- `ProcessResult` conserva i campi tecnici canonici, inclusi timing monotonic e return code nullable.
- Environment e prompt non sono enumerati o persistiti.

**Test richiesti:** Command sanitizer, clock fake, return code nullable, stdin, executable/cwd e assenza di environment serializzato.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_process_results.py tests/component/test_process_runner.py
```

**Definition of Done:** Il runner generico produce un `ProcessResult` completo senza conoscere OpenCode, Git o protocollo; comando mirato e quality gate verdi.

### M05-02

**Titolo:** Implementare drain concorrente e bounded di stdout/stderr

**Contesto:** Child e utility possono produrre output simultaneo e voluminoso; accumularlo interamente rischia deadlock o memoria non bounded.

**Obiettivo:** Drenare entrambi gli stream concorrenti verso sink/observer mantenendo limiti e diagnostica.

**Scope:** Reader concorrenti bounded; forwarding incrementale; byte count/digest; preservazione dell'output parziale; close/join bounded; buffer utility limitati.

**Fuori scope:** Interpretazione NDJSON, provider classifier, ricostruzione del protocollo e retention dei log.

**File/componenti previsti:** `src/opencode_tools/process.py`; `tests/component/test_process_runner.py`; helper sotto `tests/component/helpers/`.

**Requisiti PRD coperti:** FR-033 lato output parziale; NFR-005, NFR-006, NFR-010; AC-014, AC-032, AC-034 parziali.

**ADR applicabili:** ADR-004; ADR-008; ADR-010.

**Dipendenze:** M05-01.

**Stato bloccante:** Non è un gate dedicato; è necessario prima di timeout e adapter OpenCode.

**Acceptance criteria:**

- stdout e stderr simultanei non causano deadlock.
- Buffer e memoria restano bounded anche con output voluminoso.
- Output parziale, byte count e digest restano disponibili.
- Reader e pipe non impediscono il ritorno entro il bound.

**Test richiesti:** Stream simultanei voluminosi, partial output, reader lento o fault e chiusura bounded.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_process_results.py tests/component/test_process_runner.py
```

**Definition of Done:** Drain affidabile e limitato, senza accumulo completo o dipendenze da formati applicativi; comando mirato e quality gate verdi.

### M05-03

**Titolo:** Implementare timeout, process-group termination e cancellation

**Contesto:** Su macOS/Linux il timeout deve includere i discendenti e non può attendere indefinitamente.

**Obiettivo:** Applicare la sequenza POSIX bounded e rappresentare terminazione certa o incerta.

**Scope:** `start_new_session=True`; deadline monotonic; `SIGTERM → grace → SIGKILL → grace`; segnali al process group; close/join bounded; `termination_confirmed = false`; gestione SIGINT/SIGTERM tramite la stessa escalation prevista da SH-001.

**Fuori scope:** Quarantine e lock di M10, retry del timeout, Windows e WSL non qualificato.

**File/componenti previsti:** `src/opencode_tools/process.py`; aggiornamenti a record e port; `tests/component/test_process_runner.py`; helper descendant e TERM-resistant.

**Requisiti PRD coperti:** FR-032–FR-034; NFR-005–NFR-007; SH-001; AC-014, AC-032.

**ADR applicabili:** ADR-004; ADR-009; ADR-010; relazione con ADR-006 per il successivo uso di `termination_confirmed = false`.

**Dipendenze:** M05-01, M05-02.

**Stato bloccante:** Non è un gate dedicato; è un prerequisito tecnico di M07 e della qualification macOS/Linux di M15.

**Acceptance criteria:**

- Child e discendenti ricevono l'escalation POSIX prevista.
- Il runner restituisce controllo entro il limite anche se la terminazione non è confermabile.
- Un timeout non diventa provider retry.
- La cancellazione impedisce nuove invocation e conserva l'output parziale.

**Test richiesti:** Timeout, discendenti, TERM-resistant, cancellation, deadline con clock fake e terminazione non confermata.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_process_results.py tests/component/test_process_runner.py
```

**Definition of Done:** Nessun percorso di termination attende senza bound e lo stato di certezza è esplicito; comando mirato e quality gate verdi.

### M05-04

**Titolo:** Gestire sink fault, spawn failure e non-zero exit

**Contesto:** Failure di processo e logging devono restare categorie distinte e non autorizzare retry provider.

**Obiettivo:** Completare i failure path tecnici del runner e la propagazione typed.

**Scope:** Apertura sink prima dello spawn; sink failure termina il child e produce `LOGGING_ERROR`; executable non avviabile ed exit non-zero tecnico producono `PROCESS_ERROR`; return code nullo dove applicabile; output parziale conservato.

**Fuori scope:** Classificazione provider trusted di M07-06, finalizzazione e `run.json`, retry di process o logging error.

**File/componenti previsti:** `src/opencode_tools/process.py`; aggiornamenti a `errors.py` e `ports.py`; `tests/unit/test_process_results.py`; `tests/component/test_process_runner.py`; helper locali.

**Requisiti PRD coperti:** FR-034 e parte processuale di FR-052; NFR-005, NFR-008, NFR-010; AC-015, AC-033, AC-034 parziali.

**ADR applicabili:** ADR-004; ADR-008; ADR-009; ADR-010.

**Dipendenze:** M05-01, M05-02, M05-03.

**Stato bloccante:** Non è un gate dedicato; chiude M05 e abilita adapter e runtime store con failure path definiti.

**Acceptance criteria:**

- Spawn failure ed exit non-zero non-provider sono `PROCESS_ERROR`.
- Sink open/write fault è `LOGGING_ERROR` e nessuna di queste categorie è retryable.
- Un child attivo viene terminato bounded su sink fault.
- Dati parziali e return code nullable sono preservati.

**Test richiesti:** Executable assente, exit non-zero, sink open/write fault, partial output e assenza di provider retry.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_process_results.py tests/component/test_process_runner.py
```

**Definition of Done:** Tutti i failure path restituiscono controllo bounded, categorie corrette e dati tecnici disponibili; comando mirato e quality gate verdi.

### M06-01

**Titolo:** Validare marker terminale, ruolo/status e body

**Contesto:** Solo il messaggio assistant terminale ricostruito può guidare il lifecycle; issue, tool, reasoning e stderr sono input non fidati.

**Obiettivo:** Implementare la grammatica `agent-protocol/1` strict per marker e body.

**Scope:** Input come singolo testo normalizzato; marker exact a colonna zero, unico e terminale; matrice ruolo/status; prefissi riservati; rifiuto di `FINAL_STATUS`; body opaco e requisiti non-vuoto; `CHANGES_REQUIRED` come successo tecnico.

**Fuori scope:** Lettura NDJSON o decoding UTF-8, provider classification, decisioni state-machine/review, euristiche o code-fence extraction.

**File/componenti previsti:** `src/opencode_tools/protocol.py`; aggiornamenti a `domain.py`; `tests/unit/test_protocol.py`; `tests/fixtures/protocol/`.

**Requisiti PRD coperti:** FR-024, FR-035–FR-038, FR-047; NFR-008, NFR-011, NFR-012; AC-016, AC-017, AC-035 lato marker.

**ADR applicabili:** ADR-002; ADR-010.

**Dipendenze:** M02.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M07, M11 e M12.

**Acceptance criteria:**

- Sono accettate tutte e sole le combinazioni ruolo/status canoniche.
- Marker missing, duplicate, conflict, nonterminal, malformed o `FINAL_STATUS` producono `PROTOCOL_ERROR`.
- Il body è non vuoto dove richiesto e il testo precedente resta opaco.
- `CHANGES_REQUIRED` è parse success, non un error outcome.

**Test richiesti:** Matrice ruolo/status; casing, spazi, suffissi e newline; duplicati/conflitti; body vuoto; spoofing e `FINAL_STATUS`.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_protocol.py
```

**Definition of Done:** Parser puro, deterministico e fail-closed, senza accesso a canali raw o lifecycle; comando mirato e quality gate verdi.

### M06-02

**Titolo:** Validare ISSUE_REF_JSON v1 e identity canonica

**Contesto:** Architect READY deve consegnare un envelope versionato coerente con l'`IssueLocator` pre-risolto da Python.

**Obiettivo:** Validare schema, tipi, posizione e identity dell'envelope senza recuperare la issue in Python.

**Scope:** Una sola riga immediatamente prima di READY; sette chiavi chiuse; interi strict con bool rifiutati; stringhe difensive; match host/owner/repository/number; URL HTTPS canonico senza credential, query o fragment; titolo non vuoto.

**Fuori scope:** Fetch o verifica semantica di body/titolo GitHub, identity resolution da remote di M11, schema permissivo o migrazione v2.

**File/componenti previsti:** `src/opencode_tools/protocol.py`; aggiornamenti ai tipi issue in `domain.py`; `tests/unit/test_protocol.py`; fixture protocol.

**Requisiti PRD coperti:** FR-017, FR-018, FR-035–FR-038; NFR-008, NFR-011; AC-029, AC-035.

**ADR applicabili:** ADR-002; ADR-007; ADR-010.

**Dipendenze:** M06-01.

**Stato bloccante:** Non è un gate dedicato; chiude M06 e abilita un architect handoff verificabile.

**Acceptance criteria:**

- Un envelope valido produce un `IssueRef` coerente col locator.
- JSON/schema/type/identity/URL mismatch produce `PROTOCOL_ERROR`.
- Envelope assente su architect FAILED è ammesso.
- Architect READY richiede un handoff non vuoto.

**Test richiesti:** Golden valido e varianti malformed, extra/missing, bool, mismatch locator, URL non canonico e posizione errata.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_protocol.py
```

**Definition of Done:** Schema v1 esatto e fail-closed; Python valida l'identity senza leggere o interpretare il body issue; comando mirato e quality gate verdi.

### M07-01

**Titolo:** Costruire fixture offline OpenCode 1.17.18 con provenance

**Contesto:** OpenCode `1.17.18` è solo candidato finché adapter, fixture complete e smoke M15 non ne qualificano il supporto.

**Obiettivo:** Creare evidenza immutabile e versionata per implementare e testare l'adapter senza rete o modelli reali.

**Scope:** Fixture success/failure per tre ruoli; 429, 502, unavailable, overload/rate-limit e lookalike; malformed/truncated/multi-terminal/tool/reasoning spoofing; fallback; debug config/agent; export identity; exit non-zero e partial output; provenance/versione senza segreti.

**Fuori scope:** Dichiarare la versione supportata, smoke live, modificare fixture di future versioni o usare modelli/provider reali.

**File/componenti previsti:** `tests/fixtures/opencode/1.17.18/`; metadati di provenance; `tests/unit/test_opencode_adapter.py` per integrità e provenance iniziali; `docs/compatibility.md` con stato candidato.

**Requisiti PRD coperti:** FR-011, FR-012, FR-028, FR-035; NFR-008, NFR-009; base per AC-007, AC-016, AC-027, AC-031, AC-035.

**ADR applicabili:** ADR-002; ADR-005; ADR-009; ADR-010.

**Dipendenze:** M03, M05, M06.

**Stato bloccante:** Non è un gate dedicato; produce evidenza preliminare per l'adapter candidato. Il solo gate di qualification/supporto è M15-03.

**Acceptance criteria:**

- Il pack contiene tutti i casi positivi, negativi e di trust boundary richiesti.
- Ogni fixture è attribuita alla versione e ha provenance verificabile.
- Nessuna fixture contiene segreti.
- `docs/compatibility.md` dichiara `1.17.18` candidata, non supportata.

**Test richiesti:** Integrità e inventario fixture, caricamento offline deterministico e provenance/versione.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_opencode_adapter.py
```

**Definition of Done:** Ogni comportamento version-sensitive di M07 ha fixture immutabile positiva o negativa e la versione resta esplicitamente candidata; comando mirato e quality gate verdi.

### M07-02

**Titolo:** Implementare exact-version e capability/control-plane preflight

**Contesto:** Versione approssimativa, exit code o `--agent` non provano compatibilità né effective policy.

**Obiettivo:** Selezionare solo l'adapter exact-version e validare capability, config e agenti prima dei run.

**Scope:** Resolve executable una volta; singolo `opencode --version`; match exact dell'adapter candidato usato dalle fixture offline; `run --help`; `debug config`; tre `debug agent`; role primary, assenza di ask/task, permission baseline e sharing off; digest canonico e recheck bounded. La registry runtime delle versioni supportate non abilita `1.17.18` prima del PASS M15-03.

**Fuori scope:** Download o upgrade automatici, range semver, fallback/best effort, qualification live di M15.

**File/componenti previsti:** `src/opencode_tools/opencode.py`; `tests/unit/test_opencode_adapter.py`; `tests/component/test_opencode_preflight.py`; fixture 1.17.18; `docs/compatibility.md`.

**Requisiti PRD coperti:** FR-011, FR-012, FR-021, FR-022; NFR-008, NFR-009; SH-005; AC-007, AC-024; precondizione di AC-027.

**ADR applicabili:** ADR-005; ADR-009; ADR-010.

**Dipendenze:** M07-01.

**Stato bloccante:** Non è un gate dedicato; un preflight fallito impedisce l'invocation candidata, mentre qualification e support claim restano esclusivamente a M15-03.

**Acceptance criteria:**

- Solo un match exact seleziona l'adapter.
- Il match di M07 resta candidato/offline e non rende `1.17.18` una versione runtime supportata.
- Capability o effective policy mancanti, ambigue o più permissive falliscono chiuso.
- Tutti i ruoli risultano primary e senza ask/task.
- Un digest drift produce `OPENCODE_CONTROL_PLANE_DRIFT` prima del ruolo successivo e il raw debug non è persistito.

**Test richiesti:** Fake executable per call count, unknown version, capability mancante, agent missing/subagent/fallback, policy mismatch e digest drift.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_opencode_adapter.py tests/component/test_opencode_preflight.py
```

**Definition of Done:** Preflight fail-closed coperto offline; nessun fallback e `1.17.18` ancora candidata fino a M15; comando mirato e quality gate verdi.

### M07-03

**Titolo:** Implementare command builder OpenCode e deny-list dei flag

**Contesto:** Il workspace è il contesto OpenCode, il prompt è sensibile e modelli/provider appartengono ai file agent.

**Obiettivo:** Costruire l'invocazione OpenCode esatta e sanitizzata.

**Scope:** Executable assoluto; `run --agent <role> --format json --dir <workspace>`; cwd workspace; prompt su stdin; sessioni indipendenti; override non segreti per auto-share/update; comando persistibile col prompt redatto.

**Fuori scope:** `--auto`, `--share`, `--model`, continue/session/fork/attach, opzioni provider, prompt in argv/environment e lifecycle.

**File/componenti previsti:** `src/opencode_tools/opencode.py`; `tests/unit/test_opencode_adapter.py`.

**Requisiti PRD coperti:** FR-005 lato OpenCode, FR-009, FR-016, FR-022; AC-003 lato OpenCode, AC-023, AC-024.

**ADR applicabili:** ADR-001; ADR-005; ADR-010.

**Dipendenze:** M07-02.

**Stato bloccante:** Non è un gate dedicato; è necessario per l'esecuzione dell'adapter e per M11/M12.

**Acceptance criteria:**

- Argv esatto per ciascun ruolo con executable assoluto.
- Cwd e `--dir` indicano il workspace.
- Il prompt passa soltanto su stdin.
- Nessun flag vietato o model/provider ID appare nel comando o nella sua forma registrata.

**Test richiesti:** Casi parametrizzati per ruolo, argv/cwd/stdin/environment, sanitizer e assenza completa dei flag proibiti.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_opencode_adapter.py
```

**Definition of Done:** Builder deterministico e conforme, senza conoscenza di Git o lifecycle; comando mirato e quality gate verdi.

### M07-04

**Titolo:** Implementare decoder e transport NDJSON 1.17.18

**Contesto:** Lo stream JSON è version-sensitive e deve produrre un solo testo assistant terminale attendibile.

**Obiettivo:** Decodificare e validare il trasporto 1.17.18 prima del parser applicativo M06.

**Scope:** UTF-8 strict; sola normalizzazione CRLF/CR; NDJSON bounded; schema, event type e session ID; grouping/ordering del testo; esclusione di reasoning/tool; unico terminal assistant text; diagnostiche concorrenti secondo precedence.

**Fuori scope:** Ricerca marker in stream/tool/stderr, fallback a testo libero, identity proof via export di M07-05 e lifecycle.

**File/componenti previsti:** `src/opencode_tools/opencode.py`; `tests/unit/test_opencode_adapter.py`; fixture 1.17.18.

**Requisiti PRD coperti:** FR-018, FR-035, FR-036; NFR-008, NFR-009, NFR-011; AC-016, AC-035.

**ADR applicabili:** ADR-002; ADR-004; ADR-005; ADR-010.

**Dipendenze:** M07-01, M05, M06.

**Stato bloccante:** Non è un gate dedicato; un trasporto ambiguo impedisce la singola invocation candidata. Il gate di qualification resta M15-03.

**Acceptance criteria:**

- Una fixture success produce un solo terminal assistant text.
- UTF-8/NDJSON malformed o troncato, warning non JSON, session mismatch o più terminali producono `PROTOCOL_ERROR`.
- Tool e reasoning non entrano nel protocollo.
- I limiti impediscono crescita non bounded.

**Test richiesti:** Fixture per schema/eventi, multi-session e multi-terminal, spoofing, invalid UTF-8, truncation e dimension limits.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_opencode_adapter.py
```

**Definition of Done:** Transport adapter exact-version fail-closed e separato dal protocol parser; comando mirato e quality gate verdi.

### M07-05

**Titolo:** Verificare effective agent identity tramite export sanitizzato

**Contesto:** L'argomento `--agent` e l'exit code non dimostrano il ruolo realmente eseguito a causa del fallback possibile.

**Obiettivo:** Provare il ruolo effettivo dopo ogni invocation tramite export sanitizzato bounded.

**Scope:** Estrarre un singolo session ID; eseguire `export <session-id> --sanitize` entro utility timeout; parsing in memoria bounded; verificare ogni assistant top-level `info.agent`; registrare solo ruolo, metodo, versione e digest.

**Fuori scope:** Persistenza del raw export/config o model ID, fiducia nell'auto-attestazione agent e fallback su identity incerta.

**File/componenti previsti:** `src/opencode_tools/opencode.py`; `tests/unit/test_opencode_adapter.py`; `tests/component/test_opencode_preflight.py`; fixture export.

**Requisiti PRD coperti:** FR-011, FR-035; NFR-008, NFR-009; AC-007, AC-035.

**ADR applicabili:** ADR-002; ADR-005; ADR-010.

**Dipendenze:** M07-02, M07-04.

**Stato bloccante:** Non è un gate dedicato; senza prova identity la singola invocation fallisce chiuso e M15-03 non potrà qualificare la versione.

**Acceptance criteria:**

- Il ruolo verificato coincide col ruolo richiesto.
- Zero o più session ID, export failure/schema inatteso o role mismatch sono `PROTOCOL_ERROR`.
- Il raw export non viene persistito.
- Ogni utility call ha timeout e limiti dimensionali.

**Test richiesti:** Export agent corretto, errato o mancante; fallback; timeout/non-zero; session ambiguity e buffer limit.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_opencode_adapter.py tests/component/test_opencode_preflight.py
```

**Definition of Done:** Ogni invocation tecnicamente valida ha prova identity verificabile e ogni incertezza fallisce chiuso; comando mirato e quality gate verdi.

### M07-06

**Titolo:** Implementare classifier provider versionato

**Contesto:** Solo eventi trusted coperti da fixture possono autorizzare un provider retry.

**Obiettivo:** Classificare le signature transitorie 1.17.18 senza leggere contenuto non fidato.

**Scope:** Solo `session.error` e campi allowlisted; 429, 502, `provider_unavailable`, overload e rate limiting; `ProviderDiagnostic` con source/signature/retryable; precedence tecnica; lookalike non trusted.

**Fuori scope:** Signature configurabili CO-005, euristiche su stderr/testo, jitter CO-006 e decisione o sleep di retry.

**File/componenti previsti:** `src/opencode_tools/opencode.py`; `tests/unit/test_provider_classifier.py`; fixture 1.17.18.

**Requisiti PRD coperti:** FR-028; NFR-008, NFR-009, NFR-011; AC-011, AC-012 lato classificazione; AC-031.

**ADR applicabili:** ADR-002; ADR-004; ADR-005; ADR-010.

**Dipendenze:** M07-01, M07-04.

**Stato bloccante:** Non è un gate dedicato; è un prerequisito del retry provider. M07 resta un adapter candidato.

**Acceptance criteria:**

- Tutte le signature canoniche trusted sono riconosciute.
- Le stesse stringhe in issue, assistant, tool, reasoning o stderr non attivano retry.
- Un segnale incerto non è classificato come provider error.
- I segnali concorrenti seguono la precedence canonica.

**Test richiesti:** Fixture per ogni signature, lookalike su ogni canale non trusted e segnali concorrenti.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_provider_classifier.py
```

**Definition of Done:** Classifier deterministico, exact-version e privo di euristiche o fallback; comando mirato e quality gate verdi.

### M08-01

**Titolo:** Implementare run ID, directory layout e naming privato

**Contesto:** Ogni run e ogni attempt devono avere artifact univoci, non sovrascritti e protetti su POSIX.

**Obiettivo:** Creare il layout runtime canonico e nomi collision-safe.

**Scope:** Run ID UTC con microsecondi e 12 hex random; `runs/<run-id>` tramite mkdir esclusivo e retry bounded; nomi log con role/cycle/attempt; directory 0700 e file 0600; aperture exclusive/non-truncate e path anti-symlink.

**Fuori scope:** Retention, rotation o prune di CO-004; lock/quarantine target di M10; lifecycle e contenuto dello schema JSON.

**File/componenti previsti:** `src/opencode_tools/runlog.py`; aggiornamenti a `domain.py` e `ports.py`; `tests/unit/test_runlog_naming.py`; `tests/component/test_runtime_store.py`.

**Requisiti PRD coperti:** FR-042, FR-043, FR-046; NFR-007, NFR-008, NFR-010; AC-021, AC-034.

**ADR applicabili:** ADR-008; ADR-009; ADR-010.

**Dipendenze:** M02, M03, M05.

**Stato bloccante:** Non è un gate dedicato; è la base di M08 e prerequisito di M10/M12.

**Acceptance criteria:**

- Run ID e directory sono univoci anche con collisione simulata.
- Il naming include tutte le coordinate applicabili.
- Nessun file viene sovrascritto o troncato.
- Mode massimi 0700/0600 e path ambigui o symlink falliscono chiuso.

**Test richiesti:** Naming, run ID, collisioni, mode POSIX, mkdir/open exclusive e path/symlink safety.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_runlog_naming.py tests/component/test_runtime_store.py
```

**Definition of Done:** Layout canonico creato in modo bounded, privato e collision-safe; comando mirato e quality gate verdi.

### M08-02

**Titolo:** Implementare schema run.json v1 e serializzazione deterministica

**Contesto:** `run.json` è l'unica source of truth machine-readable e deve conservare dimensioni e cause senza appiattirle.

**Obiettivo:** Definire schema v1 e serializzazione stabile di run parziali e finali.

**Scope:** Input, config sanitizzata, environment, timing, state, Git, timeline, attempt, error, persistence e result; sequence monotone; RFC 3339 UTC; `duration_ns`; nullable canonici; cause multiple; JSON UTF-8 deterministico con newline finale.

**Fuori scope:** Usare i log come source of truth, duplicare payload raw, persistire environment completo, credential, prompt o model/provider ID e decidere il lifecycle.

**File/componenti previsti:** `src/opencode_tools/runlog.py`; record in `domain.py`; `tests/unit/test_run_schema.py`; fixture golden dello schema.

**Requisiti PRD coperti:** FR-044–FR-046; NFR-007, NFR-010, NFR-011; AC-021, AC-025 lato schema, AC-034.

**ADR applicabili:** ADR-004; ADR-008; ADR-010.

**Dipendenze:** M02, M03, M05.

**Stato bloccante:** Non è un gate dedicato; è prerequisito della persistenza atomica e della pipeline osservabile.

**Acceptance criteria:**

- Tutti i gruppi e campi minimi canonici sono rappresentabili.
- Status, outcome, Git safety, persistenza e final status restano separati.
- Nullable e cause concorrenti non si perdono.
- La serializzazione ripetuta degli stessi dati è stabile e sanitizzata.

**Test richiesti:** Golden/schema, round-trip, record parziale/finale, cause multiple, nullable, sequence, timestamp, duration e sanitizer.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_run_schema.py
```

**Definition of Done:** Schema v1 completo e deterministico, senza dati proibiti né seconda source of truth; comando mirato e quality gate verdi.

### M08-03

**Titolo:** Implementare persistenza atomica e fault handling

**Contesto:** Una write fallita non deve corrompere l'ultima JSON valida e deve impedire nuove invocation.

**Obiettivo:** Persistire ogni nuovo `RunRecord` tramite atomic replace con failure semantics fail-closed.

**Scope:** Serializzazione bounded; temp imprevedibile same-directory con `O_CREAT|O_EXCL` e mode 0600; write completa, flush/fsync, close, `os.replace`, fsync directory quando supportato; cleanup del solo temp; `LOGGING_ERROR`, artifact incomplete e stop nuove invocation.

**Fuori scope:** Sidecar alternativo, recovery o overwrite dell'ultima JSON valida, correzione automatica mode/ownership e retention.

**File/componenti previsti:** `src/opencode_tools/runlog.py`; `errors.py`; `ports.py`; `tests/component/test_runtime_store.py`; `tests/unit/test_run_schema.py`.

**Requisiti PRD coperti:** FR-044, FR-052; NFR-008, NFR-010; AC-025 lato persistenza, AC-033, AC-034.

**ADR applicabili:** ADR-004; ADR-008; ADR-009; ADR-010.

**Dipendenze:** M08-01, M08-02.

**Stato bloccante:** Non è un gate dedicato; a runtime un fault blocca nuove invocation, mentre la DoD dell'issue è un normale prerequisito di M10/M12.

**Acceptance criteria:**

- Ogni write completata lascia JSON valido.
- Fault di serialize/open/write/fsync/replace lascia intatta l'ultima versione valida.
- Dopo il fault non parte alcuna nuova invocation.
- Errore e incomplete status restano disponibili alla finalizzazione e il temp viene ripulito best effort.

**Test richiesti:** Fault injection per ogni fase, ultima JSON valida, mode temp/file, replace atomico e stop nuove invocation.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_run_schema.py tests/unit/test_runlog_naming.py tests/component/test_runtime_store.py
```

**Definition of Done:** Persistenza atomica verificata su POSIX e ogni failure produce semantica fail-closed senza corruzione; comando mirato e quality gate verdi.

### M08-04

**Titolo:** Implementare attempt log append-only e sink

**Contesto:** Ogni provider attempt richiede un log distinto e il runner deve ricevere immediatamente i fault del sink.

**Obiettivo:** Implementare log line-framed append-only sensibile e il port sink integrabile col `ProcessRunner`.

**Scope:** Apertura exclusive/non-truncate; header/footer; record timestamp, canale e payload escaped; byte non UTF-8 rappresentabili col digest raw; flush durante drain e fsync a close; fault immediato al runner; comando sanitizzato e path relativo referenziato da `run.json`.

**Fuori scope:** Parsing retroattivo dei log, uso dei log per transizioni, redazione perfetta di secret stampati dal child, retention, rotation o prune.

**File/componenti previsti:** `src/opencode_tools/runlog.py`; `ports.py`; `domain.py`; `tests/unit/test_runlog_naming.py`; `tests/component/test_runtime_store.py`; integrazione con test runner/sink M05.

**Requisiti PRD coperti:** FR-043, FR-046, FR-052; NFR-006–NFR-008, NFR-010; AC-021, AC-033, AC-034.

**ADR applicabili:** ADR-004; ADR-008; ADR-009; ADR-010.

**Dipendenze:** M08-01, M05.

**Stato bloccante:** Non è un gate dedicato; chiude M08 ed è prerequisito della logical invocation M12.

**Acceptance criteria:**

- Ogni attempt ha un file distinto e nessun record viene sovrascritto.
- Stream, canale, timing e digest sono ricostruibili dal log diagnostico.
- Un sink write fault notifica il runner e conduce a termination bounded e `LOGGING_ERROR`.
- Il raw log non guida mai la state machine.

**Test richiesti:** Log multipli, collisione/troncamento, stdout/stderr concorrenti, byte non UTF-8, flush/fsync fault, sink failure con child attivo e mode POSIX.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_runlog_naming.py tests/component/test_runtime_store.py
```

**Definition of Done:** Attempt log append-only e privato, fault propagato subito e nessun uso del log come stato applicativo; comando mirato e quality gate verdi.

## M09 — Git target safety e fingerprint `git-state-v1`

### M09-01

**Titolo:** Validare top-level Git, branch/HEAD e baseline clean

**Contesto:** La safety v0.1 richiede che ogni operazione Git sia confinata al target canonico, che il target coincida con il top-level Git e che la baseline sia determinabile e pulita.

**Obiettivo:** Implementare il preflight Git read-only del target e rifiutare ogni repository o stato iniziale non conforme.

**Scope:** Containment dopo risoluzione symlink; uguaglianza col top-level; repository non bare; branch attached; `HEAD^{commit}` risolvibile; status staged/unstaged/untracked vuoto; utility deadline; argv `git -C <target>`.

**Fuori scope:** Fingerprint content-sensitive; checkpoint per attempt; postflight; dirty mode o bypass.

**File/componenti previsti:** `src/opencode_tools/git_safety.py`; aggiornamenti mirati a `domain.py` e `ports.py`; `tests/unit/test_git_commands.py`; `tests/component/test_git_repository.py`.

**Requisiti PRD coperti:** FR-003–FR-005, FR-013–FR-014, FR-022; NFR-005, NFR-008; AC-003–AC-006, AC-036.

**ADR applicabili:** ADR-003; ADR-004; ADR-009; ADR-010.

**Dipendenze:** M02, M03, M05.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M09-02/M09-04 e della chiusura M09.

**Acceptance criteria:**

- Target fuori workspace, symlink escape, root annidato/differente e bare sono rifiutati.
- Detached/unborn e `HEAD` non risolvibile sono rifiutati.
- Staged, unstaged e untracked non ignorati rendono il preflight non valido.
- Probe timeout, non-zero o ambiguo non viene interpretato come clean.

**Test richiesti:** Command allowlist; repository temporanei clean/dirty/ignored/bare/unborn/detached/nested; symlink escape; timeout, non-zero e output ambiguo dei probe.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_git_commands.py tests/component/test_git_repository.py
```

**Definition of Done:** Nessun comando Git usa cwd/workspace implicitamente o muta lo stato; solo un target top-level, non bare, attached, con HEAD risolvibile e baseline clean supera il preflight; comando mirato e quality gate verdi.

### M09-02

**Titolo:** Implementare fingerprint git-state-v1 content-sensitive

**Contesto:** Il solo porcelain non rileva una seconda modifica a un file già `M`; il contratto congela un fingerprint versionato content-sensitive.

**Obiettivo:** Calcolare deterministicamente `git-state-v1` includendo index, path e contenuti rilevanti.

**Scope:** SHA-256 di record ordinati length-prefixed; porcelain raw; index manifest; tracked e untracked non ignorati; path raw byte-safe; tipo; contenuto o target symlink; executable bit; gitlink/submodule nel repository padre; algoritmo/versione nel record.

**Fuori scope:** Gestione delle race di campionamento; policy before/after per ruolo; benchmark di scalabilità; copie complete del codice in `run.json`.

**File/componenti previsti:** `src/opencode_tools/git_safety.py`; tipi fingerprint in `domain.py`; `tests/unit/test_git_fingerprint.py`.

**Requisiti PRD coperti:** FR-031, FR-051; NFR-008, NFR-011; AC-013, AC-028.

**ADR applicabili:** ADR-003.

**Dipendenze:** M09-01.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M09-03, M09-04 e M09-06.

**Acceptance criteria:**

- Input identico produce digest identico.
- Modifica di contenuto, mode Git-significativo, symlink target, index o untracked cambia digest.
- Una seconda modifica a un file già `M` cambia fingerprint.
- Path non UTF-8 hanno rappresentazione machine-readable byte-safe.

**Test richiesti:** Framing/hash/versione; ordering; path raw; regular/symlink/missing/other; executable bit; index e untracked; same-porcelain second edit; gitlink/submodule.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_git_fingerprint.py
```

**Definition of Done:** `git-state-v1` copre tutti i record canonici, è deterministico e rileva variazioni content-sensitive senza affidarsi al solo porcelain; comando mirato e quality gate verdi.

### M09-03

**Titolo:** Gestire stabilità di campionamento e stato INDETERMINATE

**Contesto:** Letture concorrenti o incomplete non possono produrre una falsa fotografia clean/safe.

**Obiettivo:** Rendere bounded e fail-closed l'acquisizione del fingerprint in presenza di race, file speciali ed errori.

**Scope:** `lstat` prima/dopo; ricampionamento manifest/status; un solo retry entro la stessa utility deadline; mapping di instabilità ripetuta, escape, permission error, tipo non supportato e probe incerto a `INDETERMINATE`.

**Fuori scope:** Retry provider; fallback al porcelain; attribuzione certa a un processo esterno; tuning prestazionale del gate M09-06.

**File/componenti previsti:** `src/opencode_tools/git_safety.py`; `domain.py`; `tests/unit/test_git_fingerprint.py`; `tests/component/test_git_repository.py`.

**Requisiti PRD coperti:** FR-039, FR-051; NFR-005, NFR-008; AC-028, AC-036.

**ADR applicabili:** ADR-003; ADR-004; ADR-009.

**Dipendenze:** M09-02.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M09-04 e M09-06.

**Acceptance criteria:**

- Una prima instabilità attiva al massimo un ricampionamento.
- Una seconda instabilità entro la deadline produce `INDETERMINATE`.
- Errori, escape e tipi non supportati non sono mai classificati safe.
- Ogni percorso termina entro il bound.

**Test richiesti:** Race al primo e secondo campionamento; permission failure; file speciale; path escape; deadline exhaustion; mapping outcome→`GitSafetyStatus`.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_git_fingerprint.py tests/component/test_git_repository.py
```

**Definition of Done:** Nessuna fotografia incompleta o instabile può diventare baseline/checkpoint safe e non esiste alcun loop di ricampionamento non bounded; comando mirato e quality gate verdi.

### M09-04

**Titolo:** Implementare checkpoint before/after e role mutation policy

**Contesto:** La catena Git deve restare continua prima e dopo ogni provider attempt, inclusi i percorsi di errore.

**Obiettivo:** Applicare checkpoint content-sensitive, invarianti branch/HEAD e policy di mutazione specifica per ruolo.

**Scope:** Snapshot immediatamente before/after ogni attempt; confronto del before con l'ultimo checkpoint accettato; branch/HEAD invarianti; delta architect/reviewer unsafe; delta coder riuscito accettato e inventariato; delta coder con provider error esposto al retry guard; check anche su spawn/provider/timeout/protocol failure.

**Fuori scope:** Decisione completa di provider retry; rendering change summary; recovery o rollback automatico.

**File/componenti previsti:** `src/opencode_tools/git_safety.py`; aggiornamenti a `domain.py` e `ports.py`; `tests/component/test_git_checkpoints.py`.

**Requisiti PRD coperti:** FR-020, FR-031, FR-039–FR-040, FR-050–FR-051; NFR-008, NFR-011; AC-013, AC-018, AC-028, AC-036.

**ADR applicabili:** ADR-003; ADR-004; ADR-010.

**Dipendenze:** M09-02, M09-03.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M09-05 e dell'invocation engine M12.

**Acceptance criteria:**

- Ogni attempt registra before e after anche se fallisce.
- Drift branch/HEAD blocca nuove invocation.
- Delta di architect/reviewer è unsafe.
- Modifica coder più provider error non viene nascosta e consente al retry layer di sopprimere il retry.
- Delta fra due fasi è rilevato prima del nuovo spawn.

**Test richiesti:** Drift dopo ogni ruolo; read-only-role mutation; coder success e partial mutation; failure tecniche con checkpoint; mutazione esterna fra attempt/backoff/control-plane check.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_git_fingerprint.py tests/unit/test_git_commands.py tests/component/test_git_repository.py tests/component/test_git_checkpoints.py
```

**Definition of Done:** La sequenza dei checkpoint è continua, ogni violazione degli invarianti impedisce approval e nessun percorso di errore salta il Git check previsto; comando mirato e quality gate verdi.

### M09-05

**Titolo:** Implementare change inventory e postflight preservation

**Contesto:** Il reviewer e il sommario finale devono vedere le modifiche preservate, mentre Python non deve eseguire recovery mutativa.

**Obiettivo:** Produrre inventario deterministico e postflight best effort senza alterare il target.

**Scope:** Inventario staged/tracked-unstaged/untracked; summary/path display-safe; postflight branch/HEAD/fingerprint; preservation su successo e fallimento; cause Git concorrenti; nessun contenuto completo copiato in `run.json`.

**Fuori scope:** Interpretazione della qualità del diff; rendering CLI; reset/clean/stash/checkout/rollback; attribuzione certa delle modifiche a un processo.

**File/componenti previsti:** `src/opencode_tools/git_safety.py`; tipi in `domain.py`; `tests/unit/test_git_fingerprint.py`; `tests/component/test_git_repository.py`; `tests/component/test_git_checkpoints.py`.

**Requisiti PRD coperti:** FR-023, FR-039–FR-041, FR-050; SH-007; AC-018–AC-020, AC-030.

**ADR applicabili:** ADR-003; ADR-004; ADR-010.

**Dipendenze:** M09-01, M09-04.

**Stato bloccante:** Non è un gate dedicato; completa il comportamento funzionale di M09.

**Acceptance criteria:**

- Postflight viene tentato su successo e su ogni failure inizializzata.
- Inventario distingue le tre categorie richieste.
- Drift o stato indeterminato prevalgono sull'approvazione.
- Nessun comando di recovery o mutation è costruito.

**Test richiesti:** Staged/unstaged/untracked preservation; approval-then-drift; postflight probe failure; partial coder output; audit statico command allowlist.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_git_fingerprint.py tests/unit/test_git_commands.py tests/component/test_git_repository.py tests/component/test_git_checkpoints.py
```

**Definition of Done:** Il target resta invariato da Python, tutte le modifiche osservabili sono preservate/inventariate e ogni postflight unsafe/indeterminato impedisce il final approval; comando mirato e quality gate verdi.

### M09-06

**Titolo:** [GATE BLOCCANTE M09] Qualificare la scalabilità di git-state-v1

**Contesto:** L'hashing di tracked e untracked può superare il timeout utility su repository grandi; il piano richiede evidenza, non un fallback meno sicuro.

**Obiettivo:** Misurare il fingerprint canonico su repository grandi e determinare se il timeout approvato deve essere aumentato entro i range di config.

**Scope:** Misure ripetibili su repository grandi; durata rispetto a `utility_timeout_seconds`; documentazione dell'evidenza; verifica del comportamento fail-closed; eventuale uso di un timeout maggiore già ammesso dallo schema. Le fonti canoniche non fissano dimensione del corpus o SLO ulteriori: la qualification registra il corpus usato e conclude rispetto al timeout configurato, senza inventare nuove soglie.

**Fuori scope:** Degradazione al solo porcelain; nuovo algoritmo/framing; modifica dei range canonici; ottimizzazione non giustificata da evidenza; feature Could-Have.

**File/componenti previsti:** `src/opencode_tools/git_safety.py`; `tests/unit/test_git_fingerprint.py`; `tests/component/test_git_repository.py`; sezione Evidence della issue M09-06 o artifact ad essa collegato.

**Requisiti PRD coperti:** FR-051; NFR-005, NFR-008; AC-028, AC-036.

**ADR applicabili:** ADR-003; ADR-004.

**Dipendenze:** M09-02, M09-03.

**Stato bloccante:** Sì — blocca la chiusura M09 e quindi le milestone che dichiarano M09 prerequisito; non blocca M01–M08.

**Acceptance criteria:**

- Esiste evidenza ripetibile su repository grandi.
- Il comportamento entro e fuori deadline è documentato.
- Se il default non basta, è indicato un valore maggiore entro i range approvati.
- Non esiste fallback al solo porcelain.
- L'evidenza registra descrizione non sensibile del corpus/fixture, sistema operativo, versioni Python e Git, `utility_timeout_seconds`, numero di ripetizioni, durate osservate ed esito `PASS` o `BLOCKED`.
- Un `PASS` richiede il completamento del fingerprint canonico sul corpus registrato entro il timeout configurato; in caso contrario M09 resta bloccata.

**Test richiesti:** Esecuzione del fingerprint su fixture/repository grandi nel nodo `tests/component/test_git_repository.py::test_git_state_v1_large_repository_qualification`; ripetibilità del digest; misura delle durate; scenario deadline superata con esito `INDETERMINATE`.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_git_fingerprint.py
python3.13 -m pytest tests/component/test_git_repository.py::test_git_state_v1_large_repository_qualification
```

**Definition of Done:** La scalabilità è qualificata con evidenza ripetibile nel formato richiesto e un esito esplicito; timeout e remediation restano entro il contratto canonico; nessuna soglia aggiuntiva o riduzione della copertura content-sensitive è stata introdotta; comandi mirati e quality gate verdi.

## M10 — Runtime bootstrap, target lock e quarantine

### M10-01

**Titolo:** Validare runtime root e platform/filesystem baseline

**Contesto:** Gli artifact sono privati e il runtime non deve sporcare working tree o ricadere sotto Git metadata; v0.1 supporta solo filesystem locale POSIX.

**Obiettivo:** Implementare il bootstrap fail-closed della runtime location prima della creazione degli artifact.

**Scope:** Canonicalizzazione root relativo/assoluto; directory e sentinel già ignorati se dentro working tree; rifiuto sotto Git metadata; owner corrente; no symlink; group/other bits azzerati; nessun auto-fix; gate platform/filesystem POSIX locale.

**Fuori scope:** Acquisizione lock; quarantine; modifica `.gitignore`, mode o ownership; Windows o filesystem remoto.

**File/componenti previsti:** Integrazioni in `git_safety.py`, `runlog.py` e `ports.py`; `tests/unit/test_runtime_bootstrap.py`; `tests/component/test_runtime_location.py`.

**Requisiti PRD coperti:** FR-015, FR-042; NFR-005, NFR-008, NFR-010; AC-022, AC-034.

**ADR applicabili:** ADR-003; ADR-008; ADR-009.

**Dipendenze:** M03, M05, M08, M09.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M10-02/M10-03 e del bootstrap M13.

**Acceptance criteria:**

- Runtime inside Git non ignorata e runtime sotto Git metadata sono rifiutate prima degli artifact.
- Root esistente symlink, wrong-owner o con mode permissivo fallisce senza correzioni.
- Root esterna valida è accettata.
- Piattaforma o filesystem fuori baseline fallisce chiuso.

**Test richiesti:** Runtime inside/outside/ignored/unignored; sentinel; symlink; owner/mode; path sotto metadata; platform/filesystem gate.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_runtime_bootstrap.py tests/component/test_runtime_location.py
```

**Definition of Done:** Nessuna directory o file viene creato prima della validazione e nessuna condizione non conforme viene corretta automaticamente o accettata best effort; comando mirato e quality gate verdi.

### M10-02

**Titolo:** Implementare coordination directory e target-scoped flock

**Contesto:** Due run conformi sullo stesso checkout invaliderebbero baseline e attribuzione; runtime root diverse non devono aggirare l'esclusione.

**Obiettivo:** Garantire un lease POSIX non bloccante per il singolo target fisico.

**Scope:** Coordination directory sotto absolute Git dir; mode 0700/0600; `flock` exclusive non-blocking; metadata solo dopo acquisizione; holder diagnostic; nessuna stale heuristic; lease distinti per target/worktree distinti; filesystem senza lock affidabile fail-closed.

**Fuori scope:** Lock workspace, globale o distribuito; scheduling/batch; rimozione automatica del file lock; quarantine.

**File/componenti previsti:** `src/opencode_tools/locking.py`; integrazioni in `git_safety.py` e `ports.py`; `tests/component/test_locking.py`.

**Requisiti PRD coperti:** FR-039–FR-042; NFR-005, NFR-008, NFR-010; SH-002; AC-018, AC-032, AC-034.

**ADR applicabili:** ADR-003; ADR-006; ADR-008; ADR-009.

**Dipendenze:** M10-01.

**Stato bloccante:** Non è un gate dedicato; è prerequisito della lease lifetime M13-01.

**Acceptance criteria:**

- Due processi sullo stesso target non acquisiscono entrambi il lease.
- Target differenti procedono in parallelo.
- Runtime root diverse non cambiano la chiave.
- File residuo senza lease non blocca.
- Lock non affidabile produce preflight failure.

**Test richiesti:** Multiprocess same/different target; worktree/Git dir distinti; crash e rilascio kernel; file residuo; contention metadata; lock failure; mode POSIX.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_runtime_bootstrap.py tests/component/test_locking.py tests/component/test_runtime_location.py
```

**Definition of Done:** Un solo run conforme può detenere il target, il contender fallisce prima degli agenti e nessuna euristica PID/tempo può sbloccare il lease; comando mirato e quality gate verdi.

### M10-03

**Titolo:** Implementare quarantine persistente per termination incerta

**Contesto:** Il rilascio del lock OS non prova che tutti i discendenti siano terminati quando `termination_confirmed=false`.

**Obiettivo:** Bloccare run successive finché l'utente non completa la recovery manuale prevista.

**Scope:** `quarantine-v1.json` atomica nel coordination dir prima del rilascio; mode 0600; preflight che blocca su quarantine; nessuna rimozione automatica; failure di scrittura preservata e warning che l'esclusione futura non è garantita.

**Fuori scope:** Kill o recovery aggiuntiva; stale detection PID/tempo; auto-unquarantine; sandbox o process supervisor.

**File/componenti previsti:** `src/opencode_tools/locking.py`; integrazioni mirate in `runlog.py`, `git_safety.py` e `ports.py`; `tests/component/test_locking.py`.

**Requisiti PRD coperti:** FR-033, FR-039–FR-041; NFR-005, NFR-008, NFR-010; AC-032.

**ADR applicabili:** ADR-004; ADR-006; ADR-008; ADR-009.

**Dipendenze:** M10-02.

**Stato bloccante:** Non è un gate dedicato; è obbligatoria prima dei terminal path M13.

**Acceptance criteria:**

- Termination incerta tenta la quarantine prima del lease release.
- Una quarantine presente blocca un nuovo run anche senza lock attivo.
- La quarantine non viene rimossa automaticamente.
- Failure di write mantiene final FAILED e produce diagnosi esplicita.

**Test richiesti:** Termination unconfirmed; quarantine atomica/bloccante; assenza auto-remove; write failure; crash/release; mode e path coordination.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_runtime_bootstrap.py tests/component/test_locking.py tests/component/test_runtime_location.py
```

**Definition of Done:** Ogni termination incerta osservata lascia un blocco persistente o una diagnosi esplicita di mancata garanzia, senza recovery automatica; comando mirato e quality gate verdi.

## M11 — GitHub identity, prompting e agent definition

### M11-01

**Titolo:** Risolvere repository identity target-specifica

**Contesto:** In un workspace multi-repository, cwd e repository del workspace non identificano necessariamente il target; remote assenti o ambigui devono fallire chiuso.

**Obiettivo:** Derivare deterministicamente host/owner/repository esclusivamente dal target e dagli override validati.

**Scope:** Precedenza repository override → remote esplicito → `origin` valido → unica identity GitHub; coerenza override/remote; fetch URL HTTPS, SSH e scp-like; normalizzazione `.git`, slash, host e confronto case-insensitive; GitHub.com/Enterprise; sanitizzazione credential/query/fragment.

**Fuori scope:** Preflight/auth `gh`; lettura title/body; API GitHub native; selezione dal cwd; prompt agent.

**File/componenti previsti:** `src/opencode_tools/github.py`; `tests/unit/test_github_identity.py`; `tests/component/test_github_boundary.py`.

**Requisiti PRD coperti:** FR-003–FR-005, FR-017, FR-022; NFR-008, NFR-011; AC-003, AC-029.

**ADR applicabili:** ADR-007; ADR-010.

**Dipendenze:** M03, M06, M07, M09.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M11-02/M11-03 e del preflight M13.

**Acceptance criteria:**

- Override e remote coerenti producono una sola identity.
- Mismatch, zero o più identity residue falliscono.
- `origin` non valido non prevale su un'unica identity valida alternativa.
- Credential, query e fragment non vengono persistiti.
- La repository non è mai inferita dal workspace o cwd.

**Test richiesti:** Precedence e ambiguity matrix; origin/remote esplicito/override; URL HTTPS/SSH/scp-like; Enterprise; mismatch; repository senza remote; URL sanitizer.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_github_identity.py tests/component/test_github_boundary.py
```

**Definition of Done:** Per ogni target il resolver restituisce una sola identity riproducibile o un preflight error esplicito, senza accesso alla issue né dipendenza dal cwd; comando mirato e quality gate verdi.

### M11-02

**Titolo:** Implementare gh preflight e IssueLocator-only boundary

**Contesto:** Python deve provare disponibilità/autenticazione GitHub, ma la lettura e comprensione della issue resta responsabilità dell'architect.

**Obiettivo:** Validare `gh` per l'host risolto e costruire soltanto un `IssueLocator` coerente con target e numero positivo.

**Scope:** Preflight bounded di versione e auth host-specifica; diagnosi su executable/version/auth; persistenza solo di metadata/digest sanitizzati; `IssueLocator` con identity e numero.

**Fuori scope:** Fetch Python di title/body; API GitHub native; mutation GitHub; seconda fetch di verifica; interpretazione semantica della issue.

**File/componenti previsti:** `src/opencode_tools/github.py`; tipi e port mirati; `tests/unit/test_github_identity.py`; `tests/component/test_github_boundary.py`.

**Requisiti PRD coperti:** FR-017–FR-018, FR-022; NFR-005, NFR-008; AC-029.

**ADR applicabili:** ADR-002; ADR-007; ADR-010.

**Dipendenze:** M11-01.

**Stato bloccante:** Non è un gate dedicato; è prerequisito del prompt architect e del preflight M13.

**Acceptance criteria:**

- `gh` assente, versione non parsabile, host non configurato o auth invalida/scaduta falliscono prima dell'architect.
- Raw auth non viene persistito.
- Locator e URL atteso sono target-specifici.
- Python non legge title o body.

**Test richiesti:** Fake `gh` per version/auth success/failure/timeout; github.com/Enterprise; issue number positivo; assenza fetch/API native e raw auth nei record.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_github_identity.py tests/component/test_github_boundary.py
```

**Definition of Done:** Un locator valido esiste prima dell'architect solo dopo preflight `gh` riuscito e il boundary Python resta limitato a identity e numero; comando mirato e quality gate verdi.

### M11-03

**Titolo:** Costruire prompt canonicali per architect, coder e reviewer

**Contesto:** Issue, handoff e feedback sono dati non fidati e opachi e non devono ridefinire ruolo, safety o protocollo.

**Obiettivo:** Costruire prompt deterministici che trasportino integralmente tutti gli input canonici dei tre ruoli.

**Scope:** Delimiter trusted; prompt architect con issue number, workspace/target, identity e `gh issue view`; prompt coder con `IssueRef`, handoff integrale, target, cycle e policy; prompt reviewer con issue/handoff/report coder, inventario/diff/test scope e target read-only; feedback integrale dopo la sola normalizzazione prevista; protocol marker/envelope richiesti.

**Fuori scope:** Riassunto o interpretazione Python; scelta di piano/modello/provider; fetch issue Python; decisione dello status; esecuzione agent.

**File/componenti previsti:** `src/opencode_tools/prompting.py`; `tests/unit/test_prompting.py`.

**Requisiti PRD coperti:** FR-010, FR-016–FR-020, FR-023; NFR-008, NFR-011; AC-009, AC-024, AC-029–AC-030.

**ADR applicabili:** ADR-001; ADR-002; ADR-003; ADR-007; ADR-010.

**Dipendenze:** M11-01, M06.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M11-04, M12 e M13.

**Acceptance criteria:**

- Ogni ruolo riceve soltanto gli input previsti.
- Il prompt architect contiene la command shape trusted esatta `gh issue view <number> --repo <identity>` costruita dal locator target-specifico.
- Issue, handoff e feedback restano integrali e delimitati.
- Marker in dati non fidati non diventano control plane.
- Il reviewer riceve staged, tracked-unstaged, untracked e test scope.
- Nessun model/provider ID entra nel builder.

**Test richiesti:** Golden prompt per ruolo/cycle; command capture di `gh issue view <number> --repo <identity>` nel prompt architect; opaque payload; newline normalization; marker injection nella issue; reviewer input completo; target/workspace distinti; assenza model ID.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_prompting.py
```

**Definition of Done:** I prompt sono deterministici, completi e role-specifici e Python non interpreta né riassume alcun payload agent/issue; comando mirato e quality gate verdi.

### M11-04

**Titolo:** Definire i tre agenti e verificarne la permission matrix

**Contesto:** V0.1 ammette soltanto tre agent primary e richiede che effective policy e write scope siano verificabili e mai più permissivi del baseline.

**Obiettivo:** Creare le definizioni project-local e coprire staticamente ed effectively ruolo, modelli e permission policy.

**Scope:** `architect`, `coder`, `reviewer` primary; nessun `orchestrator`, `ask` o `task`; modelli solo nei file agent; architect/reviewer read-only; coder edit solo target; staging, mutation Git/GitHub e share negati; fixture/linter della matrice; compatibilità con `debug agent` e digest effective config.

**Fuori scope:** Scelta o configurazione modelli in Python/TOML; sandbox OS; quarto agent; subagent; mutation o publication; qualification live 1.17.18.

**File/componenti previsti:** `.opencode/agents/architect.md`; `.opencode/agents/coder.md`; `.opencode/agents/reviewer.md`; `tests/unit/test_agent_policy.py`; integrazione in `tests/component/test_github_boundary.py`.

**Requisiti PRD coperti:** FR-010–FR-011, FR-020–FR-022; NFR-008, NFR-011; AC-007, AC-023–AC-024.

**ADR applicabili:** ADR-001; ADR-003; ADR-005; ADR-010.

**Dipendenze:** M07, M11-03.

**Stato bloccante:** Non è un gate dedicato; è prerequisito della logical invocation, della pipeline e della qualification M15-03.

**Acceptance criteria:**

- Esistono esattamente i tre agent primary.
- `ask`, `task` e azioni proibite sono negate.
- Architect/reviewer non possono editare e coder è limitato al target.
- Cambiare model ID nei file agent non richiede cambi Python/TOML.
- Effective policy più permissiva fallisce.

**Test richiesti:** Policy allow/deny completa; agent count/names/mode; fake `debug agent`; control-plane digest; assenza model ID nel package/TOML; command inventory proibita.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_github_identity.py tests/unit/test_prompting.py tests/unit/test_agent_policy.py tests/component/test_github_boundary.py
```

**Definition of Done:** Static config ed effective config dimostrano i tre ruoli e la least privilege prevista, senza introdurre orchestrazione o configurazione modello nel control plane Python; comando mirato e quality gate verdi.

## M12 — Logical invocation engine

### M12-01

**Titolo:** Implementare skeleton port-only della logical invocation

**Contesto:** L'orchestrator deve coordinare una singola invocation senza costruire adapter concreti, subprocess o service locator.

**Obiettivo:** Definire il flusso applicativo e i record minimi di una logical invocation tramite sole porte iniettate.

**Scope:** Prima struttura di `orchestrator.py`; injection dei port; logical invocation ID; ruolo/phase/cycle/provider attempt distinti; sequenze/event record; result tipizzato; fake che registrano call.

**Fuori scope:** Pipeline completa; retry/rework; composition root; adapter concreti; rendering CLI.

**File/componenti previsti:** `src/opencode_tools/orchestrator.py`; integrazioni minime in `state_machine.py`, `retry.py` e `prompting.py`; `tests/unit/test_attempt_precedence.py`; `tests/component/test_orchestrator_attempts.py`.

**Requisiti PRD coperti:** FR-027, FR-034, FR-045; NFR-006, NFR-011–NFR-012; AC-011 e AC-021 a livello strutturale.

**ADR applicabili:** ADR-001; ADR-004.

**Dipendenze:** M04–M11.

**Stato bloccante:** Non è un gate dedicato; è prerequisito delle altre issue M12.

**Acceptance criteria:**

- L'orchestrator importa solo domain, port e policy pure.
- Ogni invocation ha identity e contatori non ambigui.
- I fake osservano ordine e input.
- Nessun adapter o subprocess viene istanziato nel modulo.

**Test richiesti:** Import-boundary; dependency injection; ID/counter invariants; call recording; costruzione del result con campi ortogonali.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_attempt_precedence.py tests/component/test_orchestrator_attempts.py
```

**Definition of Done:** Una invocation controllata è componibile interamente con fake e il modulo non contiene I/O concreto né reasoning; comando mirato e quality gate verdi.

### M12-02

**Titolo:** Integrare checkpoint, control-plane check, process e protocol precedence

**Contesto:** Ogni provider attempt deve seguire un ordine fisso e classificare senza ambiguità process outcome, protocollo e Git safety.

**Obiettivo:** Implementare la sequenza canonica dell'attempt e la precedence tecnica completa.

**Scope:** Log esclusivo; snapshot before continuo; recheck digest control-plane; agent run; snapshot/check after anche su failure; provider classifier; precedence `TIMEOUT → PROVIDER_ERROR → PROCESS_ERROR → PROTOCOL_ERROR → AGENT_REPORTED_FAILURE → SUCCEEDED`; parsing protocol solo se ammesso; cause concorrenti e Git/persistence status separati.

**Fuori scope:** Review pipeline; rendering CLI; retry sleep/exhaustion completo; adapter concreti nell'orchestrator.

**File/componenti previsti:** `src/opencode_tools/orchestrator.py`; integrazioni in `state_machine.py` e `prompting.py`; `tests/unit/test_attempt_precedence.py`; `tests/component/test_orchestrator_attempts.py`.

**Requisiti PRD coperti:** FR-028, FR-032–FR-041, FR-050–FR-051; NFR-005–NFR-008, NFR-011–NFR-012; AC-014–AC-018, AC-028, AC-031–AC-032, AC-035–AC-036.

**ADR applicabili:** ADR-001–ADR-005; ADR-009; ADR-010.

**Dipendenze:** M12-01.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M12-03/M12-04 e M13.

**Acceptance criteria:**

- L'ordine delle operazioni è osservabile e invariabile.
- Ogni failure effettua il Git check previsto.
- Outcome superiore non viene sostituito ma conserva diagnostiche concorrenti.
- Protocol parser non viene chiamato quando un outcome superiore lo vieta.
- Control-plane drift blocca prima del ruolo successivo.

**Test richiesti:** Precedence esaustiva; fake call order; timeout/process/provider/protocol/agent failure; read-only mutation; branch/HEAD drift; control-plane drift; Git probe indeterminato; nessun parsing dopo outcome superiore.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_attempt_precedence.py tests/component/test_orchestrator_attempts.py
```

**Definition of Done:** Ogni attempt produce una classificazione deterministica e un trace completo, senza saltare checkpoint o confondere outcome tecnico, Git safety e protocol status; comando mirato e quality gate verdi.

### M12-03

**Titolo:** Persistire attempt distinti e gestire logging/cancellation

**Contesto:** Ogni attempt deve essere ricostruibile; una failure del sink/store deve interrompere nuove invocation e un interrupt deve restare bounded.

**Obiettivo:** Collegare invocation, attempt log, `run.json`, error records e cancellation alla sequenza applicativa.

**Scope:** Record separato per attempt; naming/sequence/cycle/attempt; atomic persistence dopo eventi materiali; source/signature/delay fields; logging failure fail-fast e child termination; ultima JSON valida preservata; cancellation prima, durante o nel sleep; persistence status ortogonale.

**Fuori scope:** Resume; ricostruzione dai raw log; retention/prune; rendering CLI; retry provider decision completa.

**File/componenti previsti:** `src/opencode_tools/orchestrator.py`; integrazioni mirate in `retry.py`; `tests/unit/test_attempt_precedence.py`; `tests/component/test_orchestrator_attempts.py`.

**Requisiti PRD coperti:** FR-034, FR-043–FR-046, FR-052; NFR-005–NFR-008, NFR-011–NFR-012; SH-001; AC-021, AC-032–AC-033 a livello invocation.

**ADR applicabili:** ADR-001; ADR-004; ADR-006; ADR-008; ADR-009.

**Dipendenze:** M12-01, M12-02.

**Stato bloccante:** Non è un gate dedicato; è prerequisito del retry M12-04 e della pipeline M13.

**Acceptance criteria:**

- Retry/rework non sovrascrivono log o record.
- Persistence failure impedisce nuove call e termina un child attivo.
- L'ultima JSON valida resta disponibile e può risultare incompleta.
- Cancellation impedisce attempt successivi.
- Environment, prompt e raw config non entrano in `run.json`.

**Test richiesti:** Multi-attempt record uniqueness; store/sink open-write-fsync-replace fault; cancellation idle/active/sleep; partial output; cause concorrenti; privacy serialization come regressione del contratto M08, non come nuova copertura di AC-034.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_attempt_precedence.py tests/component/test_orchestrator_attempts.py
```

**Definition of Done:** Ogni attempt resta distinguibile e persistito per quanto possibile e logging/cancellation terminano il flusso senza attese infinite o nuove invocation; comando mirato e quality gate verdi.

### M12-04

**Titolo:** Integrare provider retry guard ed exhaustion

**Contesto:** Solo un provider error trusted può essere ritentato e il retry non deve consumare review cycle o ripetere dopo una mutazione coder.

**Obiettivo:** Applicare tutti i guard, persistere backoff e chiudere deterministicamente l'exhaustion.

**Scope:** Attempt da 1 per logical invocation; trusted retryable diagnostic; budget; termination confermata; Git safe; persistence OK; no cancellation; fingerprint coder invariato; delay capped planned/actual; persistenza prima/dopo sleep; exhaustion `PROVIDER_ERROR`; suppression su coder mutation.

**Fuori scope:** Retry di timeout/process/protocol/agent/Git/logging failure; jitter; review rework; sleep reale nei test.

**File/componenti previsti:** `src/opencode_tools/orchestrator.py`; `src/opencode_tools/retry.py`; `tests/unit/test_attempt_precedence.py`; `tests/component/test_orchestrator_attempts.py`.

**Requisiti PRD coperti:** FR-027–FR-031; NFR-005–NFR-006, NFR-011; AC-011–AC-013, AC-021, AC-031.

**ADR applicabili:** ADR-003; ADR-004; ADR-005.

**Dipendenze:** M12-02, M12-03.

**Stato bloccante:** Non è un gate dedicato; completa la Definition of Done M12 ed è prerequisito di M13.

**Acceptance criteria:**

- Trusted 429/502 può ritentare nello stesso phase/cycle.
- Lookalike non trusted non ritenta.
- Exhaustion usa esattamente il budget e non consuma cycle.
- Coder mutation sopprime retry e preserva modifiche.
- Write failure o cancellation impedisce sleep e nuovo attempt.

**Test richiesti:** Guard matrix; recovery ed exhaustion; exact/capped delay con fake clock/sleeper; coder mutation; termination/persistence/Git unsafe/cancelled; source/signature persistite.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_attempt_precedence.py tests/component/test_orchestrator_attempts.py
```

**Definition of Done:** Nessun outcome non-provider è retryable, ogni retry rispetta tutti i guard e l'exhaustion conserva outcome, contatori e modifiche canonici; comando mirato e quality gate verdi.

## M13 — Pipeline single-issue end-to-end con fake

### M13-01

**Titolo:** Comporre bootstrap, preflight e lease lifetime

**Contesto:** M12 rende disponibile una logical invocation completa tramite port; M13 deve inserirla nel lifecycle single-issue, distinguendo i controlli che possono precedere la run dagli errori successivi all'inizializzazione e mantenendo l'esclusione sul target per tutta la finestra critica.

**Obiettivo:** Comporre inizializzazione, preflight completo, baseline Git e lease target-scoped nell'ordine vincolante del lifecycle.

**Scope:** Ricevere `RunRequest` e configurazione già validati; eseguire il bootstrap read-only necessario alla runtime; acquisire il lease prima della baseline; creare la run directory e il primo `RunRecord`; risolvere una sola volta l'`IssueLocator`; eseguire una sola volta i preflight platform, runtime, Git, GitHub e OpenCode, inclusi versione, capability, effective agent e digest del control plane; mantenere il lease fino a dopo la finalizzazione; impedire qualunque invocation agent su failure.

**Fuori scope:** Parsing/rendering CLI; implementazione degli adapter già coperti da M05–M11; esecuzione dei ruoli; review/retry loop; qualification live di OpenCode `1.17.18`; supporto Windows o filesystem remoti; euristiche di sblocco stale.

**File/componenti previsti:** `src/opencode_tools/orchestrator.py`; aggiornamenti strettamente necessari alle transition policy in `src/opencode_tools/state_machine.py`; `tests/component/test_pipeline_failures.py`; `tests/component/test_pipeline_multirepo.py`; integrazione tramite i port già definiti per run store, lease, Git safety, issue resolution e agent runner.

**Requisiti PRD coperti:** FR-005, FR-010–FR-015, FR-017, FR-020–FR-022, FR-039, FR-042, FR-044; NFR-005–NFR-006, NFR-008–NFR-010, NFR-012; SH-002. Validation point: AC-003, AC-007, AC-029, AC-036.

**ADR applicabili:** ADR-001; ADR-003–ADR-010.

**Dipendenze:** M12.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M13-02–M13-05. Non qualifica OpenCode `1.17.18`, che resta subordinato a M15-03.

**Acceptance criteria:**

- Il lease viene acquisito prima della baseline e resta detenuto fino al completamento della finalizzazione.
- Runtime, baseline, identity GitHub e compatibility/effective-agent preflight sono eseguiti nell'ordine previsto e una sola volta per run.
- Viene risolto un solo `IssueLocator` dal target; Python non legge il body della issue.
- Una failure preflight non avvia architect, coder o reviewer e, se la run è già inizializzata, entra nel percorso terminale comune.
- Il workspace resta il contesto OpenCode e ogni operazione Git/diff mantiene il target esplicito.
- Lock contention, quarantine esistente, Git indeterminato o control plane ambiguo falliscono chiuso senza correzioni automatiche.

**Test richiesti:** Component test con port/fake deterministici per ordine bootstrap/preflight, singola risoluzione issue, lease acquisition/release, lock contention, quarantine, Git probe failure, control-plane failure e fixture multi-repository. Nessun nuovo unit test di policy; eventuali regressioni pure restano nel modulo owner.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/component/test_pipeline_failures.py tests/component/test_pipeline_multirepo.py
```

**Definition of Done:** Il lifecycle inizializza una run in modo sicuro, acquisisce il lease prima della baseline, completa un solo preflight fail-closed e conserva il lease fino alla finalizzazione; nessun agent parte su preflight non verde e comando mirato più quality gate passano.

### M13-02

**Titolo:** Comporre happy path architect → coder → reviewer

**Contesto:** Con bootstrap e preflight verdi, Python deve essere l'unica autorità che invoca direttamente i tre ruoli e trasporta gli output opachi senza introdurre un quarto agent o interpretare la semantica della issue.

**Obiettivo:** Comporre il percorso nominale `architect → coder(1) → reviewer(1)` con envelope, handoff, inventario e checkpoint integri.

**Scope:** Invocare architect, coder e reviewer come agenti primari in sessioni indipendenti; richiedere `READY` ed `ISSUE_REF_JSON` valido prima del coder; passare integralmente handoff e issue ref al coder; richiedere `COMPLETED` prima del reviewer; passare al reviewer issue ref, handoff, body/report coder, inventario corrente e target canonico; acquisire checkpoint e persistere gli eventi dopo ogni attempt; tradurre `APPROVED` nell'azione verso postflight senza emettere output CLI.

**Fuori scope:** Rework e provider retry, coperti da M13-03; convergenza dei terminal path e gate finale, coperti da M13-04; analisi Python del body issue, handoff, feedback, diff o sufficienza dei test; nuovi ruoli, sessione persistente, model selection o mutation Git/GitHub.

**File/componenti previsti:** `src/opencode_tools/orchestrator.py`; aggiornamenti strettamente necessari in `src/opencode_tools/state_machine.py`; `tests/component/test_single_issue_pipeline.py`; integrazione dei prompt e delle logical invocation già completati in M11–M12.

**Requisiti PRD coperti:** FR-005, FR-010, FR-016–FR-023, FR-035–FR-039, FR-042–FR-045, FR-047–FR-050; NFR-005–NFR-006, NFR-008, NFR-010–NFR-012. Validation point: AC-003, AC-007–AC-008, AC-021, AC-029–AC-030.

**ADR applicabili:** ADR-001–ADR-005; ADR-007–ADR-010.

**Dipendenze:** M13-01.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M13-03–M13-05.

**Acceptance criteria:**

- Le logical invocation osservate sono esattamente architect, coder ciclo 1 e reviewer ciclo 1, nell'ordine canonico.
- Coder non viene invocato senza architect `READY` e `ISSUE_REF_JSON` coerente con il locator.
- Reviewer non viene invocato senza coder `COMPLETED` e riceve tutti gli input previsti, incluso l'inventario dei nuovi file non ignorati.
- Handoff e body/report agent restano opachi e vengono trasportati integralmente; nessun dato non fidato controlla il lifecycle.
- Ogni attempt mantiene checkpoint, record e log distinti; una mutation di architect/reviewer impedisce la fase successiva.
- Un reviewer `APPROVED` produce l'azione verso postflight, non un'approvazione anticipata né un `FINAL_STATUS` agent.

**Test richiesti:** Component test del percorso nominale e delle sue precondizioni con fake registranti ordine, ruolo, cwd, argv, stdin, timeout e payload; casi negativi per architect non READY/envelope invalido, coder non COMPLETED, mutation read-only e reviewer input incompleto. Nessun provider o GitHub reale.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/component/test_single_issue_pipeline.py
```

**Definition of Done:** Il percorso nominale invoca soltanto i tre ruoli nell'ordine previsto, conserva envelope/handoff/inventario senza interpretarli e porta `APPROVED` al postflight mantenendo checkpoint e persistenza coerenti; comando mirato e quality gate verdi.

### M13-03

**Titolo:** Comporre review/rework loop e provider retry per ciclo

**Contesto:** Review cycle e provider attempt sono contatori indipendenti: `CHANGES_REQUIRED` apre un nuovo ciclo logico, mentre un provider retry resta nello stesso ruolo, phase e ciclo e non deve ripetere lavoro dopo una mutazione coder.

**Obiettivo:** Integrare il review/rework loop e il retry provider separato senza coder extra, perdita di feedback o deviazioni dai guard già congelati.

**Scope:** Avviare `coder(n+1)` solo dopo `CHANGES_REQUIRED` con `n < max_review_cycles`; passare integralmente il feedback al coder successivo; invocare `reviewer(n)` dopo ogni coder `COMPLETED`; terminare con `REVIEW_CYCLES_EXHAUSTED` all'ultimo ciclo senza altro coder; mantenere i retry provider nello stesso ruolo/ciclo; non richiamare coder durante retry reviewer; applicare budget, backoff capped e tutti i guard M12; persistere decisioni/delay; sopprimere retry coder dopo delta e preservare le modifiche.

**Fuori scope:** Jitter, retry di timeout/process/protocol/agent/Git/logging error, resume, rollback, timeout per ruolo, interpretazione del feedback o nuovo ciclo dopo exhaustion; final rendering CLI.

**File/componenti previsti:** `src/opencode_tools/orchestrator.py`; aggiornamenti mirati alle transition/finalization policy in `src/opencode_tools/state_machine.py`; `tests/component/test_single_issue_pipeline.py`; `tests/component/test_pipeline_failures.py`; riuso senza duplicazione delle policy `retry.py` e della logical invocation M12.

**Requisiti PRD coperti:** FR-019, FR-024–FR-031, FR-039, FR-043–FR-045, FR-050–FR-052; NFR-005–NFR-006, NFR-008, NFR-011–NFR-012. Validation point: AC-009–AC-013, AC-021, AC-030–AC-031.

**ADR applicabili:** ADR-001–ADR-005; ADR-008; ADR-010.

**Dipendenze:** M13-02.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M13-04 e M13-05.

**Acceptance criteria:**

- `CHANGES_REQUIRED` incrementa soltanto il review cycle e il feedback integrale raggiunge il coder successivo.
- `CHANGES_REQUIRED` all'ultimo ciclo produce `REVIEW_CYCLES_EXHAUSTED` senza invocare un coder aggiuntivo.
- Un retry provider incrementa soltanto l'attempt e resta nella stessa phase, ruolo e cycle.
- Un retry reviewer non richiama il coder; recovery ed exhaustion usano esattamente il budget configurato.
- Un provider error coder con delta mantiene `PROVIDER_ERROR`, registra la suppression e non ritenta né ripristina il target.
- Cancellation, persistence non OK, termination non confermata, Git non safe o diagnostica non trusted impediscono sleep e nuovo attempt.

**Test richiesti:** Component test per rework poi approval, limite al primo e ultimo ciclo, retry recovery/exhaustion per ciascun ruolo, lookalike non trusted, delay con fake clock/sleeper, reviewer retry senza coder, coder partial mutation, persistence/cancellation/Git/termination guard e unicità dei record.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/component/test_single_issue_pipeline.py tests/component/test_pipeline_failures.py
```

**Definition of Done:** Rework e provider retry seguono loop, contatori e guard distinti, nessun feedback viene alterato e nessun coder extra è invocato dopo exhaustion o durante retry reviewer; comando mirato e quality gate verdi.

### M13-04

**Titolo:** Convergere terminal path in postflight e finalizzazione

**Contesto:** Dopo l'inizializzazione, successo, failure, interruption e cause concorrenti devono raggiungere un unico percorso terminale best effort; un `APPROVED` storico non può prevalere su Git drift, persistenza fallita o terminazione incerta.

**Obiettivo:** Rendere postflight, precedence, quarantine, preservation e finalizzazione comuni a ogni terminal path inizializzato.

**Scope:** Portare ogni outcome terminale a postflight best effort e finalizzazione; ricontrollare branch, `HEAD` e fingerprint; conservare trigger outcome, cause concorrenti e precedence; applicare il gate finale completo; produrre esattamente un `FinalStatus` nel risultato Python; preservare modifiche e change inventory su successo/fallimento; gestire interruption; creare quarantine prima del rilascio lease quando la terminazione non è confermata; persistere l'ultima versione possibile di `run.json` e rilasciare il lease solo dopo la finalizzazione.

**Fuori scope:** Rendering stdout/stderr ed exit process, coperti da M14; reset, clean, stash, checkout, rollback o cleanup automatici; recovery automatica dalla quarantine; ricostruzione dai raw log; console JSON o resume.

**File/componenti previsti:** `src/opencode_tools/orchestrator.py`; aggiornamenti mirati in `src/opencode_tools/state_machine.py`; `tests/component/test_single_issue_pipeline.py`; `tests/component/test_pipeline_failures.py`; integrazione dei port Git safety, run store e lease già implementati.

**Requisiti PRD coperti:** FR-033–FR-034, FR-039–FR-050, FR-052; NFR-005–NFR-008, NFR-010–NFR-012; SH-001–SH-002. Validation point: AC-017–AC-021, AC-025, AC-028, AC-032–AC-033, AC-036.

**ADR applicabili:** ADR-001–ADR-010.

**Dipendenze:** M13-01, M13-02, M13-03.

**Stato bloccante:** Non è un gate dedicato; è prerequisito della qualification M13-05.

**Acceptance criteria:**

- Ogni path terminale successivo alla run init tenta postflight e finalizzazione senza avviare ulteriori agenti.
- `FinalStatus.APPROVED` è possibile solo dopo reviewer approval, postflight safe, branch/HEAD invariati e persistenza OK.
- Git unsafe/indeterminate o logging failure prevalgono nel terminal outcome senza cancellare le cause precedenti.
- Provider exhaustion, timeout, process/protocol/agent failure, review exhaustion e interruption restano categorie distinguibili e finalizzano `FAILED`.
- Modifiche complete o parziali e relativo inventario restano nel target senza recovery mutativa.
- Terminazione non confermata produce postflight indeterminato e quarantine prima del rilascio lease; una failure di quarantine resta visibile.
- `IssueResult` e l'ultima `run.json` valida sono coerenti per quanto la persistenza consente e contengono un solo final status Python.

**Test richiesti:** Component test per tutti i terminal outcome, cause concorrenti e precedence; approval-then-drift; postflight e atomic persistence failure; interruption; preservation/inventory; termination non confermata e quarantine failure; verifica che ogni path inizializzato raggiunga il finalizer e rilasci il lease nell'ordine corretto.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/component/test_single_issue_pipeline.py tests/component/test_pipeline_failures.py tests/component/test_pipeline_multirepo.py
```

**Definition of Done:** Tutti i terminal path inizializzati convergono in postflight/finalizer, il gate finale non approva su drift o persistenza non OK, le modifiche sono preservate e lease/quarantine rispettano l'ordine canonico; comando mirato e quality gate verdi.

### M13-05

**Titolo:** Qualificare failure matrix e multi-repository con fake

**Contesto:** La composizione M13 è accettabile soltanto se l'intera pipeline e i failure path sono riproducibili senza rete, provider, GitHub reale o backoff reale, incluso il caso in cui workspace e target repository non coincidono.

**Obiettivo:** Produrre l'evidenza deterministica completa della milestone M13 senza aggiungere comportamento applicativo.

**Scope:** Completare le suite component per happy path, rework, review exhaustion, provider recovery/exhaustion per ogni ruolo, coder partial mutation, terminal outcomes, approval-then-drift, postflight failure, persistence failure, interrupt, control-plane drift, lock/quarantine e multi-repository; verificare ordine, contatori, call capture, target/workspace, singolo issue locator, soli tre agenti, preservation e coerenza `IssueResult`/ultima `run.json` valida.

**Fuori scope:** Smoke live, rete, credenziali, provider/modelli reali, `gh` reale, attese di backoff reali, qualification OpenCode `1.17.18`, matrice macOS/Linux, benchmark fingerprint, feature nuove o correzioni che cambino i contratti congelati.

**File/componenti previsti:** `tests/component/test_single_issue_pipeline.py`; `tests/component/test_pipeline_failures.py`; `tests/component/test_pipeline_multirepo.py`; eventuali sole correzioni aderenti al contratto in `src/opencode_tools/orchestrator.py` e `src/opencode_tools/state_machine.py` se la matrice espone difetti della composizione M13.

**Requisiti PRD coperti:** FR-005, FR-010–FR-052 come prova di composizione, con focus FR-016, FR-019, FR-025–FR-031, FR-039–FR-041, FR-047–FR-050; NFR-005–NFR-012. Validation point: AC-003, AC-007–AC-013, AC-017–AC-021, AC-025, AC-028–AC-033, AC-036.

**ADR applicabili:** ADR-001–ADR-010.

**Dipendenze:** M13-01, M13-02, M13-03, M13-04.

**Stato bloccante:** Non è uno dei tre gate dedicati; completa la Definition of Done M13 ed è prerequisito di M14. Presuppone già superato il gate fingerprint M09-06 e non chiude i gate M15-02/M15-03.

**Acceptance criteria:**

- I punti di validazione M13 nominati dal piano hanno entry point riconoscibili e passano con fake deterministici.
- Ogni transition e failure path previsto è coperto senza rete, credential, provider/modelli reali o sleep reale.
- Il fixture multi-repository prova OpenCode nel workspace e Git/diff sul target esplicito.
- Le call registrate mostrano un solo issue locator, nessun quarto agent e nessun comando/flag mutativo vietato.
- Retry e review producono record distinti e contatori indipendenti; mutation e cause concorrenti restano osservabili con la precedence prevista.
- Ogni run inizializzata produce un `IssueResult` coerente con l'ultima `run.json` valida e preserva le modifiche.
- La suite non riqualifica la scalabilità fingerprint già richiesta da M09-06 e non dichiara qualificati OpenCode `1.17.18` o la matrice macOS/Linux, riservati a M15.

**Test richiesti:** Tutti i component test M13 indicati dal piano, con scripted agent/process, fake clock/sleeper, recording/faulting store e sink, fake lease e repository Git temporanei; replay ripetuto deve produrre transizioni, classificazioni, contatori e final status identici.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/component/test_single_issue_pipeline.py tests/component/test_pipeline_failures.py tests/component/test_pipeline_multirepo.py
```

**Definition of Done:** La matrice M13 passa interamente con fake, copre ogni transition/failure path e il caso multi-repository, dimostra i vincoli su locator/agenti/mutation e non anticipa i gate di evidenza M15; comando mirato e quality gate verdi.

## M14 — CLI, composition root e documentazione operatore

### M14-01

**Titolo:** Implementare comando e parser CLI single-issue

**Contesto:** La pipeline validata deve essere esposta come comando locale non interattivo con una sola issue scalare e senza override che allarghino il lifecycle o introducano batch.

**Obiettivo:** Implementare la command shape canonica e separare parsing/entrypoint dalla composizione e dalla business logic.

**Scope:** Esporre `opencode-tools run --workspace <path> --target <relative|.> --issue <positive-int> [--config <path>]` con `argparse`; aggiungere entry point e delega `python -m opencode_tools`; richiedere tutti gli argomenti obbligatori; accettare un solo intero positivo; rifiutare liste, range, batch, target assoluti e override lifecycle individuali; mantenere gli errori sintattici prima della run con exit 2 e senza promessa di artifact o final marker.

**Fuori scope:** Interpretazione TOML e path safety già coperte da M03; costruzione degli adapter e invocation della pipeline, coperte da M14-02; rendering finale, M14-03; documentazione operatore, M14-04; subcommand validate/dry-run/inspect, prompt interattivi, JSON console o multi-issue.

**File/componenti previsti:** `src/opencode_tools/cli.py`; `src/opencode_tools/__main__.py`; entry point in `pyproject.toml`; `tests/unit/test_cli.py`.

**Requisiti PRD coperti:** FR-001–FR-002, FR-006; NFR-001, NFR-003–NFR-004, NFR-012. Validation point: AC-001, AC-026.

**ADR applicabili:** ADR-001; ADR-008–ADR-009.

**Dipendenze:** M01–M13.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M14-02–M14-04.

**Acceptance criteria:**

- Il comando canonico e `python -m opencode_tools` espongono la stessa interfaccia `run`.
- Workspace, target, issue e config hanno esclusivamente la forma prevista; issue zero/negativa, lista, range e batch sono rifiutati.
- Target assoluti e override di singoli parametri lifecycle non sono accettati dalla CLI.
- `__main__.py` delega a `cli.py` e non contiene parsing o business logic.
- Un errore `argparse` usa exit 2 prima della run e non emette `FINAL_STATUS` né promette `run.json`.
- L'help documenta soltanto opzioni e command shape v0.1.

**Test richiesti:** Unit test parser positivo/negativo, issue scalar, argomenti mancanti, target assoluto, opzioni sconosciute, entrypoint e help; verifica che gli errori sintattici non costruiscano la pipeline né emettano marker.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_cli.py
PYTHONPATH=src python3.13 -m opencode_tools --help
```

**Definition of Done:** La CLI accetta soltanto una richiesta single-issue valida, rifiuta ogni forma fuori contratto prima della run e mantiene `__main__.py` privo di logica; comando mirato, help e quality gate verdi.

### M14-02

**Titolo:** Implementare composition root e port wiring

**Contesto:** `cli.py` è l'unico punto autorizzato a caricare configurazione, costruire gli adapter concreti e iniettare i port; l'orchestrator non deve conoscere implementazioni low-level o dipendere dal cwd globale.

**Obiettivo:** Collegare parser, config, adapter e `IssueOrchestrator` in una composition root esplicita e testabile.

**Scope:** Trasformare gli argomenti in `RunRequest` e `AppConfig`; caricare `--config`, file convenzionale o default tramite il loader M03; costruire e iniettare process runner, clock/sleeper, run store/sink, Git safety, GitHub identity, lock, OpenCode runner e prompt boundary; invocare una sola pipeline; mantenere workspace come cwd OpenCode e target esplicito per Git/diff; preservare i confini pre-init/post-init; evitare service locator, singleton, model/provider/credential Python e flag OpenCode vietati.

**Fuori scope:** Reimplementare lifecycle, state machine, adapter o protocollo; business logic in `cli.py`; accesso Python al body issue; rendering/exit mapping M14-03; documentazione M14-04; smoke reale e qualification M15.

**File/componenti previsti:** `src/opencode_tools/cli.py`; `tests/unit/test_cli.py`; `tests/component/test_cli_end_to_end.py`; wiring degli adapter concreti già implementati senza modificarne i contratti.

**Requisiti PRD coperti:** FR-005–FR-006, FR-009–FR-010, FR-022, FR-042, FR-046; NFR-002–NFR-003, NFR-006, NFR-008–NFR-010, NFR-012. Validation point: AC-003, AC-007, AC-021, AC-023–AC-024, AC-034.

**ADR applicabili:** ADR-001; ADR-004–ADR-010.

**Dipendenze:** M14-01.

**Stato bloccante:** Non è un gate dedicato; è prerequisito di M14-03 e M14-04.

**Acceptance criteria:**

- `cli.py` è l'unico composition root e costruisce/inietta adapter tramite port senza inserirli nell'orchestrator.
- Ogni comando valido carica una sola configurazione effettiva e invoca una sola pipeline single-issue.
- OpenCode riceve il workspace come cwd; Git, diff e identity resolution ricevono sempre il target esplicito.
- Errori config/bootstrap precedenti alla creazione sicura della run non promettono artifact; risultati inizializzati sono consegnati al renderer con il relativo artifact path.
- Command/config capture non contiene model ID, credential, prompt in argv, environment completo o flag `--auto`, `--share`, `--model`.
- Nessun adapter o boundary Python costruisce mutation Git/GitHub e nessuna logica di lifecycle viene duplicata nella CLI.

**Test richiesti:** Unit test di composition wiring con factory/fake e failure pre-init; component test CLI con fake OpenCode/`gh`, repository temporanei, config source e workspace=target/multi-repo; call capture per dipendenze, cwd, target, argv, stdin e assenza di mutation/flag vietati.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_cli.py tests/component/test_cli_end_to_end.py
PYTHONPATH=src python3.13 -m opencode_tools --help
```

**Definition of Done:** La composition root costruisce e inietta tutti i port una sola volta, esegue la pipeline senza duplicare business logic e conserva separazione workspace/target, privacy e command policy; comando mirato, help e quality gate verdi.

### M14-03

**Titolo:** Implementare final rendering, exit code e summary

**Contesto:** La CLI deve offrire un contratto terminale semplice da automatizzare: un solo marker Python dopo la run init, dettagli su stderr e exit code stabili coerenti con l'outcome e con l'ultima persistenza valida.

**Obiettivo:** Tradurre errori pre-init e `IssueResult` inizializzati in stdout, stderr ed exit code senza alterare il lifecycle.

**Scope:** Gestire errori pre-init senza final marker; dopo init emettere esattamente una riga `FINAL_STATUS: APPROVED|FAILED` su stdout; mappare gli exit 0/2/10/20/30/40/130 secondo il System Design; rendere su stderr phase, outcome, remediation e warning artifact incompleto; includere summary display-safe con run ID, artifact path e modifiche staged/tracked-unstaged/untracked; mostrare diagnosi compatibility con versione rilevata/supportata e remediation; mantenere coerenza con `run.json` quando la write finale riesce.

**Fuori scope:** Cambiare precedence, outcome o final gate; aggiungere output JSON console, UI/TUI, prompt interattivi o inspect-run; stampare raw log, environment, prompt, credential o effective config; interpretare diff o qualità delle modifiche.

**File/componenti previsti:** `src/opencode_tools/cli.py`; `tests/unit/test_cli.py`; `tests/component/test_cli_end_to_end.py`.

**Requisiti PRD coperti:** FR-042, FR-046–FR-050; NFR-003–NFR-004, NFR-008, NFR-010, NFR-012; SH-003–SH-005, SH-007. Validation point: AC-007, AC-021, AC-025–AC-026, AC-034.

**ADR applicabili:** ADR-001; ADR-004–ADR-006; ADR-008–ADR-010.

**Dipendenze:** M14-02.

**Stato bloccante:** Non è un gate dedicato; è prerequisito della documentazione M14-04 e della chiusura M14.

**Acceptance criteria:**

- Ogni run inizializzata emette su stdout una e una sola riga `FINAL_STATUS`, coerente con `IssueResult` ed exit code.
- Exit 0 è riservato ad `APPROVED`; le famiglie 10/20/30/40 e l'exit 130 pre-init seguono la tabella canonica senza appiattire gli outcome.
- Errori `argparse` e SIGINT prima dell'inizializzazione non promettono artifact o final marker.
- Stderr include run ID, ultima phase, terminal outcome, artifact path noto e change summary raggruppata senza interpretare il diff.
- Compatibility failure nomina versione rilevata, baseline supportata e remediation senza fallback.
- Logging failure segnala esplicitamente che l'artifact può essere incompleto; raw dati sensibili non vengono riversati in console.

**Test richiesti:** Unit test della matrice exit/final status, stdout count, stderr summary, compatibility diagnosis, pre-init eccezioni e artifact-incomplete warning; component test happy/failure per coerenza CLI/`IssueResult`/`run.json`, preservation summary e output stream separation.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_cli.py tests/component/test_cli_end_to_end.py
PYTHONPATH=src python3.13 -m opencode_tools --help
```

**Definition of Done:** Pre-init e run inizializzate seguono i rispettivi contratti; marker, exit code, stderr summary e `run.json` sono coerenti per quanto la persistenza consente e non espongono payload sensibili; comando mirato, help e quality gate verdi.

### M14-04

**Titolo:** Completare documentazione operatore conforme al comportamento provato

**Contesto:** La documentazione M14 deve descrivere soltanto comportamento implementato e provato, rendendo espliciti safety, privacy, recovery e limiti di compatibilità senza anticipare i gate di release M15.

**Obiettivo:** Allineare README e documenti operatore al comando, agli artifact e alle garanzie effettive della v0.1.

**Scope:** Documentare comando single-issue, setup e quattro comandi QG; distinzione workspace/target, target clean e runtime ignore; dipendenze esterne e configurazione senza model/provider credential; final marker, exit code, summary e artifact path; raw log sensibili, mode POSIX, limiti di redazione e retention manuale; recovery manuale da failure, modifiche preservate, lock e quarantine senza reset/cleanup automatico; baseline macOS/Linux su filesystem locale POSIX e piattaforme escluse; compatibility exact-version come non qualificata finché M15-03 non passa; azioni vietate e threat model cooperativo senza promessa di sandbox.

**Fuori scope:** Dichiarare PASS per qualification OpenCode, matrice macOS/Linux o gate non ancora eseguiti; supporto Windows/WSL su filesystem Windows; retention/prune, resume, inspect-run, dry-run/config validate, console JSON, batch, pubblicazione automatica, rollback o nuove garanzie di containment.

**File/componenti previsti:** `README.md`; `docs/security-and-privacy.md`; `docs/recovery.md`; aggiornamento di `docs/compatibility.md`; riscontri in `tests/unit/test_cli.py` e `tests/component/test_cli_end_to_end.py` per il comportamento descritto.

**Requisiti PRD coperti:** FR-001–FR-002, FR-005–FR-006, FR-009–FR-010, FR-022, FR-042, FR-046–FR-050; NFR-001–NFR-004, NFR-008–NFR-010, NFR-012; SH-003–SH-007. Validation point: AC-001, AC-003, AC-007, AC-021, AC-023–AC-026, AC-034.

**ADR applicabili:** ADR-001; ADR-004–ADR-010.

**Dipendenze:** M14-01, M14-02, M14-03.

**Stato bloccante:** Non è un gate dedicato; completa la Definition of Done M14 ed è prerequisito di M15. I gate M15-02 e M15-03 restano aperti e non bloccano milestone precedenti.

**Acceptance criteria:**

- README riporta il comando canonico, il perimetro single-issue e i quattro comandi QG esatti.
- La documentazione distingue workspace e target, richiede baseline clean/runtime ignorata e non attribuisce al programma mutation o recovery automatiche.
- Security/privacy descrive mode `0700/0600`, sensibilità dei raw log, assenza di environment/credential deliberati nei payload e limiti della redazione.
- Recovery descrive preservation, ispezione manuale, lock attivo e quarantine senza euristiche stale, rollback o cleanup automatico.
- Compatibility dichiara solo macOS/Linux su filesystem locale POSIX e non presenta OpenCode `1.17.18` come supportato prima del gate M15-03.
- I documenti non promettono sandbox, Windows, batch, resume, retention automatica, model selection Python o publication/mutation Git/GitHub.
- Comando, output, exit code, artifact e remediation descritti coincidono con i test M14 verdi.

**Test richiesti:** Esecuzione dei test CLI canonici e audit statico di README/documenti contro command shape, output, exit matrix, privacy, recovery, piattaforme e compatibility provati; verifica esplicita dell'assenza di claim Could-Have, fuori scope o supporto non qualificato.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/unit/test_cli.py tests/component/test_cli_end_to_end.py
PYTHONPATH=src python3.13 -m opencode_tools --help
```

**Definition of Done:** README, security/privacy, recovery e compatibility riflettono esclusivamente comportamento provato, mantengono visibili limiti e gate ancora aperti e non introducono Could-Have o garanzie fuori scope; comando mirato, help e quality gate verdi.

## M15 — Acceptance completa e qualification di release

### M15-01

**Titolo:** [RELEASE BLOCKER M15] Chiudere la matrice deterministica AC-001–AC-036

**Contesto:** M15 non introduce nuove feature: deve trasformare la matrice di validazione canonica in evidenza di release verificabile. Lo smoke live AC-027 resta separato in M15-03 e la seconda piattaforma resta separata in M15-02; nessuno dei due può essere sostituito dalla suite offline.

**Obiettivo:** Eseguire e revisionare tutta l'evidenza deterministica dei Must-Have e dei 36 acceptance scenario, chiudendo soltanto gap di prova o difetti rispetto ai contratti già congelati.

**Scope:** Eseguire tutti gli entry point AC-001–AC-036 nominati nell'Implementation Plan; rieseguire unit, component e fixture offline; verificare replay identico delle fixture; svolgere gli audit statici di import boundary, command inventory, model separation e dipendenze; registrare per ogni AC evidenza e risultato senza waiver. Per AC-027 questa issue verifica solo le precondizioni deterministiche; la prova live conclusiva appartiene a M15-03.

**Fuori scope:** Smoke OpenCode reale; matrice sulla seconda piattaforma; dichiarazione di supporto per `1.17.18`; nuove feature, modifica dei contratti canonici, Could-Have, fallback, rete/credential/provider reali nei test ordinari.

**File/componenti previsti:** Suite sotto `tests/unit/` e `tests/component/`; `tests/fixtures/opencode/1.17.18/`; test di import/command/model/dependency audit; matrice AC-001–AC-036 dell'Implementation Plan come indice dell'evidenza.

**Requisiti PRD coperti:** Gate di release per FR-001–FR-052, NFR-001–NFR-012, US-001–US-013, AC-001–AC-036 e SH-001–SH-007; la componente live di AC-027 resta a M15-03.

**ADR applicabili:** ADR-001–ADR-010.

**Dipendenze:** M14.

**Stato bloccante:** Sì — blocca la release v0.1; non blocca M01–M14. M15-02 e M15-03 sono gate di release paralleli, non dipendenze implementative di questa issue.

**Acceptance criteria:**

- Ogni riga AC-001–AC-036 ha un entry point riconoscibile, evidenza e risultato esplicito; AC-027 resta aperto fino a M15-03.
- Tutta la suite deterministica e le fixture offline passano senza rete, credential, provider/modelli reali o backoff reale.
- Il replay delle fixture è identico e gli audit statici non trovano dipendenze inverse, comandi proibiti, model ID nel control plane o dipendenze Python runtime non previste.
- Un fallimento non viene convertito in waiver, best effort o fallback; resta bloccante finché il contratto congelato non è soddisfatto.
- Nessun fix di qualification introduce comportamento nuovo, Could-Have o fuori scope.

**Test richiesti:** Tutti gli unit e component test associati agli AC-001–AC-036; replay delle fixture OpenCode versionate; audit statici import/command/model/dependency; full suite deterministica.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest
```

**Definition of Done:** La componente deterministica della matrice AC-001–AC-036 è PASS con evidenza, senza waiver né regressioni di scope; il `QG` è verde. La release resta comunque bloccata finché anche M15-02 e M15-03 non sono PASS.

### M15-02

**Titolo:** [RELEASE BLOCKER M15] Eseguire la matrice macOS/Linux POSIX

**Contesto:** Process group, segnali, `flock`, mode `0700/0600`, `fsync` e semantica dei path sono garanzie di safety platform-specifiche. Un PASS su una sola piattaforma non qualifica macOS e Linux insieme.

**Obiettivo:** Dimostrare che la suite deterministica e il quality gate passano separatamente su macOS e Linux, entrambi su filesystem locale POSIX.

**Scope:** Eseguire l'intero `QG` e la suite acceptance deterministica su entrambe le piattaforme; verificare in particolare process-group termination, interruption, lock/quarantine, mode, atomic replace/directory sync e path Git byte-safe; registrare piattaforma, release, data e risultati senza dump dell'environment; riportare in `docs/compatibility.md` soltanto combinazioni realmente provate.

**Fuori scope:** Windows native; WSL su filesystem montato da Windows; filesystem remoto/non POSIX; adapter Windows; smoke OpenCode live di M15-03; dedurre una piattaforma dall'esito dell'altra; allargare lo scope di supporto.

**File/componenti previsti:** Suite sotto `tests/unit/` e `tests/component/`; test POSIX di processi, locking, runtime store e Git safety; `docs/compatibility.md`.

**Requisiti PRD coperti:** FR-033, FR-046, FR-052; NFR-001, NFR-004–NFR-005, NFR-008, NFR-010; SH-001–SH-002; AC-014, AC-026, AC-032 e AC-034.

**ADR applicabili:** ADR-004; ADR-006; ADR-008; ADR-009.

**Dipendenze:** M14.

**Stato bloccante:** Sì — blocca la release v0.1 se una delle due righe della matrice manca o fallisce; non blocca M01–M14.

**Acceptance criteria:**

- Il `QG` completo passa in un ambiente macOS supportato su filesystem locale POSIX.
- Il `QG` completo passa in un ambiente Linux supportato su filesystem locale POSIX.
- I test platform-sensitive coprono termination/cancellation, lock/quarantine, mode, atomicità/durability e path senza degradazione best effort.
- Evidenza, data e combinazioni realmente provate sono registrate; una riga non eseguita non è marcata PASS.
- Windows, WSL su mount Windows e filesystem remoti non vengono dichiarati supportati.

**Test richiesti:** Full suite su macOS e Linux; focus sui test process group/termination, `flock`, quarantine, runtime mode/atomic replace e Git path/fingerprint.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:** Da eseguire integralmente e separatamente su macOS e Linux POSIX locali.

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Definition of Done:** Entrambe le righe macOS e Linux sono PASS con evidenza e `QG` verde; `docs/compatibility.md` dichiara soltanto combinazioni provate. Un singolo PASS, un ambiente fuori baseline o una degradazione best effort non chiudono la issue.

### M15-03

**Titolo:** [RELEASE BLOCKER M15] Qualificare OpenCode 1.17.18

**Contesto:** `1.17.18` è soltanto un adapter candidato finché fixture complete con provenance e smoke AC-027 non provano transport, capability ed effective agent identity. `--agent` ed exit code non bastano perché il fallback può essere silenzioso.

**Obiettivo:** Qualificare oppure respingere la versione esatta OpenCode `1.17.18` senza fallback, e abilitarne il support claim solo dopo evidenza positiva completa.

**Scope:** Verificare completezza, immutabilità e provenance del fixture pack; eseguire lo smoke opt-in con versione esatta, target disposable clean, issue fixture controllata, agent project-local, sharing disabilitato e nessuna publication/mutation GitHub; verificare capability, `debug config`, i tre `debug agent`, session ID unico e sanitized export con `info.agent` coerente per i tre ruoli; ispezionare gli artifact; soltanto dopo PASS abilitare `1.17.18` nella registry exact-version e documentarla come supportata.

**Fuori scope:** Versioni diverse da `1.17.18`; range semver o `latest`; fallback JSON→testo, agent richiesto→default, versione sconosciuta→best effort o classifier incerto→retry; download/login/credential management; model ID in Python/TOML; publication, commit o mutation GitHub.

**File/componenti previsti:** `tests/fixtures/opencode/1.17.18/`; `tests/integration/test_opencode_1_17_18_smoke.py`; `src/opencode_tools/opencode.py`; `.opencode/agents/architect.md`; `.opencode/agents/coder.md`; `.opencode/agents/reviewer.md`; `docs/compatibility.md`.

**Requisiti PRD coperti:** FR-009, FR-011–FR-012, FR-021–FR-022, FR-028, FR-035; NFR-008–NFR-009; US-013; SH-005; AC-007, AC-023–AC-024, AC-027, AC-031 e AC-035.

**ADR applicabili:** ADR-002; ADR-004; ADR-005; ADR-008–ADR-010.

**Dipendenze:** M14.

**Stato bloccante:** Sì — blocca sia il support claim `1.17.18` sia la release v0.1; non blocca M01–M14.

**Acceptance criteria:**

- Il fixture pack copre success/failure per ruolo, provider trusted/lookalike, stream malformed/truncated, terminal ambiguity/spoofing, fallback e identity corretta/errata/mancante, con provenance/versione e senza secret.
- Lo smoke usa esattamente OpenCode `1.17.18`, un target disposable clean, agent locali e sharing disabilitato.
- Capability e effective permission matrix sono valide per `architect`, `coder` e `reviewer`; debug/export e session identity provano il ruolo realmente eseguito.
- Gli artifact sono ispezionati e non si osservano publication, commit o mutation GitHub.
- Solo dopo PASS registry e `docs/compatibility.md` dichiarano `1.17.18` supportata.
- Se fixture, identity proof o smoke falliscono, la versione resta non supportata, la release resta bloccata e ADR-005 viene riesaminato; non viene introdotto alcun fallback.

**Test richiesti:** Replay completo delle fixture versionate; test component di preflight/transport/classifier/identity; smoke live AC-027 opt-in e disposable; ispezione degli artifact e audit delle azioni vietate.

**Quality gate:**

```bash
python3.13 -m pytest
ruff check .
ruff format --check .
mypy --strict src tests
```

**Validation commands:**

```bash
python3.13 -m pytest tests/integration/test_opencode_1_17_18_smoke.py -m live
```

**Definition of Done:** Fixture e provenance sono complete, `QG` e smoke AC-027 passano, agent identity è provata per i tre ruoli e gli artifact sono stati ispezionati; solo allora registry e compatibilità qualificano la versione esatta. Qualunque esito diverso mantiene adapter e release bloccati, senza fallback.

## Audit finale di copertura e scope

### Copertura milestone — 15/15

| Milestone | Issue previste | Prerequisiti canonici | Gate dedicati |
|---|---|---|---|
| M01 | M01-01–M01-03 | Nessuno | Nessuno |
| M02 | M02-01–M02-04 | M01 | Nessuno |
| M03 | M03-01–M03-04 | M02 | Nessuno |
| M04 | M04-01–M04-03 | M02, M03 | Nessuno |
| M05 | M05-01–M05-04 | M02, M03 | Nessuno |
| M06 | M06-01–M06-02 | M02 | Nessuno |
| M07 | M07-01–M07-06 | M03, M05, M06 | Adapter candidato; qualification a M15-03 |
| M08 | M08-01–M08-04 | M02, M03, M05 | Nessuno |
| M09 | M09-01–M09-06 | M02, M03, M05 | M09-06 blocca M09 e dipendenti |
| M10 | M10-01–M10-03 | M03, M05, M08, M09 | Nessuno |
| M11 | M11-01–M11-04 | M03, M06, M07, M09 | Nessuno |
| M12 | M12-01–M12-04 | M04–M11 | Nessuno |
| M13 | M13-01–M13-05 | M12 | Nessuno |
| M14 | M14-01–M14-04 | M01–M13 | Nessuno |
| M15 | M15-01–M15-03 | M14 | Tutte e tre le issue bloccano la release |

**Esito:** 15/15 milestone e 59 issue complessive rappresentate. La catena critica e le dipendenze del piano sono preservate; i gate M09-06, M15-02 e M15-03 non bloccano milestone anteriori a quelle indicate.

### Copertura FR — 52/52

L'inventario è il range chiuso e senza lacune FR-001–FR-052. Ogni requisito mantiene la milestone di implementazione e il punto AC assegnati dalla matrice §4 dell'Implementation Plan; le schede delle issue riportano i relativi ID nel campo **Requisiti PRD coperti**. M15-01 li sottopone tutti al gate cumulativo di release e non ne introduce di nuovi.

**Esito:** 52/52 FR assegnati; nessun FR orfano, rinumerato o reinterpretato.

### Copertura NFR — 12/12

L'inventario è NFR-001–NFR-012. Packaging/runtime, dipendenze, typing, quality, boundedness, testability, time, fail-closed, compatibility, privacy, determinismo e maintainability hanno issue implementative nelle milestone canoniche; M15-01 chiude il gate cumulativo, M15-02 qualifica la baseline POSIX e M15-03 qualifica la compatibilità OpenCode.

**Esito:** 12/12 NFR assegnati e verificabili.

### Copertura user story — 13/13

L'inventario è US-001–US-013. Le issue M03–M14 realizzano i comportamenti integrati e gli acceptance point indicati nella matrice §5.2 dell'Implementation Plan; M15 conserva il gate finale, con US-013 collegata esplicitamente alla qualification M15-03.

**Esito:** 13/13 user story raggiungono almeno un comportamento integrato e un acceptance point.

### Copertura acceptance — 36/36

L'inventario è AC-001–AC-036 e ogni riga conserva l'entry point nominato nella matrice §6 dell'Implementation Plan. M15-01 raccoglie l'evidenza deterministica; AC-026 richiede il `QG` su macOS e Linux tramite M15-02; AC-027 richiede lo smoke live separato tramite M15-03. Nessuno smoke sostituisce un test deterministico.

**Esito:** 36/36 AC collocati; la release richiede PASS effettivo, non il solo placement progettuale.

### Copertura ADR — 10/10

| ADR | Milestone/issue di esecuzione principale | Verifica finale |
|---|---|---|
| ADR-001 | M02, M04, M11–M14 | M15-01 |
| ADR-002 | M06–M07, M11–M13 | M15-01, M15-03 |
| ADR-003 | M09, M12–M13 | M09-06, M15-01 |
| ADR-004 | M04–M05, M12–M13 | M15-01–M15-03 |
| ADR-005 | M07, M11, M13 | M15-03 condizionale, senza fallback |
| ADR-006 | M10, M13 | M15-01, M15-02 |
| ADR-007 | M03, M06, M11, M13 | M15-01 |
| ADR-008 | M08, M10, M14 | M15-01–M15-03 |
| ADR-009 | M01, M05, M08, M10 | M15-02 e ambiente M15-03 |
| ADR-010 | M06–M11, M14 | M15-01, M15-03 |

**Esito:** 10/10 ADR applicati; ADR-005 resta esplicitamente condizionale alla prova e deve essere riesaminato se la qualification fallisce.

### Should-Have inclusi — SH-001–SH-007

| Requirement | Collocazione preservata |
|---|---|
| SH-001 — interruption | M05, M12–M13; verifica M15-01/M15-02 |
| SH-002 — lock per target | M10, M13; verifica M15-01/M15-02 |
| SH-003 — console summary | M14; verifica M15-01 |
| SH-004 — exit code stabili | M04, M14; verifica M15-01 |
| SH-005 — compatibility diagnosis | M07, M14; verifica M15-03 |
| SH-006 — privacy documentation | M14; verifica M15-01 |
| SH-007 — change summary | M09, M14; verifica M15-01 |

**Esito:** SH-001–SH-007 inclusi; nessuno è perso o differito implicitamente.

### Could-Have esclusi — CO-001–CO-008

| Requirement | Disposizione confermata |
|---|---|
| CO-001 — timeout per ruolo | Escluso; resta un timeout OpenCode globale |
| CO-002 — config validate/dry-run | Escluso |
| CO-003 — console JSON | Escluso; `run.json` resta la source of truth |
| CO-004 — retention/rotation | Escluso; cleanup manuale documentato |
| CO-005 — signature provider configurabili | Escluso; allowlist versionata |
| CO-006 — jitter | Escluso; backoff deterministico |
| CO-007 — inspect run | Escluso |
| CO-008 — resume | Escluso; ogni rilancio crea una nuova run |

**Esito:** CO-001–CO-008 assenti dallo scope implementativo e non usati per chiudere alcun gate.

### Non-obiettivi assenti

- Nessun batch, range, coda o invocation concorrente multi-issue; il lock per target è safety, non scheduling.
- Nessun quarto agent `orchestrator` né seconda state machine fuori da Python.
- Nessun commit/amend/tag/branch, push/force-push/merge/rebase, reset/clean/stash/checkout distruttivo, rollback o mutation GitHub.
- Nessuna apertura/modifica/merge PR e nessuna modifica/assegnazione/label/commento/chiusura issue.
- Nessun worktree-per-issue, batch isolation, orchestrazione remota/multi-host, servizio/daemon, API/webhook, database/coda, UI/web UI/TUI/dashboard o plugin system.
- Nessuna selezione modello, gestione credential/provider o garanzia di disponibilità modello nel control plane Python.
- Nessun commit o pubblicazione automatica degli artifact.
- Nessuna promessa di correttezza semantica AI o sandbox contro processi deliberatamente ostili.
- Windows native, WSL su filesystem Windows e filesystem remoti restano fuori dalla matrice v0.1.

**Esito:** nessun non-obiettivo PRD è stato trasformato in issue implementativa.

### Gate aperti e impatto

| Gate | Collocazione | Criterio di chiusura | Impatto se non PASS |
|---|---|---|---|
| Scalabilità `git-state-v1` | M09-06 | Evidenza ripetibile su repository grandi; eventuale timeout maggiore solo entro range approvati; nessun fallback al porcelain | Blocca M09 e i suoi dipendenti; non blocca M01–M08 |
| Matrice deterministica release | M15-01 | AC-001–AC-036 con evidenza deterministica, audit statici e `QG` verde; AC-027 resta alla prova live | Blocca la release; non blocca M01–M14 |
| Matrice macOS/Linux POSIX | M15-02 | Full suite e `QG` PASS su entrambe le piattaforme, filesystem locale POSIX | Blocca la release; un solo sistema operativo non qualifica l'altro; non blocca M01–M14 |
| Qualification OpenCode `1.17.18` | M15-03 | Fixture complete/provenance, identity proof e smoke AC-027 PASS; registry/docs aggiornati solo dopo | Blocca support claim e release; `1.17.18` resta non supportata, ADR-005 va riesaminato e non è ammesso fallback; non blocca M01–M14 |

**Verdetto finale dell'audit:** coverage strutturale completa — milestone 15/15, FR 52/52, NFR 12/12, US 13/13, AC 36/36, ADR 10/10, SH 7/7; CO 0/8 introdotti e non-obiettivi assenti. Questo è un audit di placement e tracciabilità, non attesta che i gate d'implementazione o release siano già PASS.
