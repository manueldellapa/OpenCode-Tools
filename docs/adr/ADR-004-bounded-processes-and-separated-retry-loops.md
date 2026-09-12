# ADR-004: Process group, timeout e loop retry separati

- **Stato:** Accettato
- **Data:** 2026-09-11
- **Ambito:** OpenCode-Tools v0.1
- **Fonti canoniche:** [PRD](../../tasks/prd-opencode-tools.md), [System Design](../../tasks/system-design-opencode-tools.md)

## Contesto

OpenCode e le utility possono bloccarsi, generare discendenti o fallire mentre producono output. Inoltre un tentativo tecnico contro un provider e una decisione logica del reviewer hanno semantiche diverse: un contatore condiviso renderebbe imprevedibile il numero di revisioni e potrebbe ripetere lavoro dopo modifiche parziali.

## Decisione

`ProcessRunner` è una porta generica, indipendente da OpenCode, che usa argv strutturati con `shell=False`, cwd per-child, prompt su stdin e drain concorrente bounded di stdout/stderr.

Su piattaforme supportate ogni child usa un nuovo process group. Alla deadline si applica una sequenza finita `TERM → grace → KILL → grace`, seguita da chiusura e join bounded. Il runner ritorna anche quando non può confermare la terminazione; in quel caso la pipeline fallisce e applica la quarantine di ADR-006. Interruzioni utente riusano la stessa semantica.

I due loop restano distinti:

- `provider_attempt` riparte da 1 per ogni invocazione logica di ruolo e aumenta soltanto per un `PROVIDER_ERROR` trusted;
- `review_cycle` parte da 1, aumenta soltanto dopo `CHANGES_REQUIRED` e conta decisioni del reviewer;
- timeout, process/protocol error, agent failure, Git/logging error e interrupt non vengono convertiti in retry provider;
- un retry richiede `provider_diagnostic.retryable = true`, terminazione confermata, Git safe, persistenza valida, budget residuo, nessuna cancellazione richiesta e, per il coder, fingerprint invariato;
- backoff e tutti i loop hanno limiti finiti e clock/sleeper iniettati.

I valori numerici iniziali e i relativi range appartengono allo schema di configurazione versionato, non a un ADR separato. Questo ADR congela la semantica dei contatori, dei guard e della terminazione.

## Conseguenze

- Il massimo numero di processi e il worst case temporale sono derivabili dalla configurazione.
- Retry provider non consumano review cycle; rework non si confonde con un errore infrastrutturale.
- Output parziale e cause concorrenti restano diagnosticabili.
- La gestione robusta dei discendenti è legata alla baseline POSIX di ADR-009.

## Alternative scartate

- Un unico `max_retries` per provider e reviewer.
- Retry di timeout o process error come se fossero provider failure.
- Accumulo completo dell'output in memoria o prompt in argv.
- Terminazione del solo PID principale o attese senza bound.

## Quando riesaminare

Riesaminare se cambiano la piattaforma supportata, le categorie retryable o la relazione tra logical invocation, provider attempt e review cycle. Modificare soltanto i default numerici non richiede un nuovo ADR.

## Tracciabilità

PRD FR-025–FR-034, FR-039, FR-043–FR-045, FR-052; NFR-005–NFR-007; SH-001; OQ-001; AC-010–AC-015, AC-020, AC-021, AC-032, AC-033; System Design §§10.1–10.2, 12 e 13.
