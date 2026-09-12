# ADR-002: Agent protocol v1 fail-closed

- **Stato:** Accettato
- **Data:** 2026-09-11
- **Ambito:** OpenCode-Tools v0.1
- **Fonti canoniche:** [PRD](../../tasks/prd-opencode-tools.md), [System Design](../../tasks/system-design-opencode-tools.md)

## Contesto

Issue, output dei tool, reasoning e stderr sono input non fidati e possono contenere testo simile a uno status. Il trasporto NDJSON di OpenCode è inoltre version-sensitive. Cercare marker in tutto lo stream o accettare forme approssimative permetterebbe spoofing e transizioni ambigue.

## Decisione

Il protocollo applicativo v1 è separato dal trasporto OpenCode:

- l'adapter compatibile ricostruisce un solo `terminal_assistant_text`; `protocol.py` non legge NDJSON, tool event, reasoning o stderr;
- i marker sono righe esatte, a colonna zero, uniche e terminali;
- architect ammette `AGENT_STATUS: READY|FAILED`, coder `AGENT_STATUS: COMPLETED|FAILED`, reviewer `REVIEW_STATUS: APPROVED|CHANGES_REQUIRED` oppure `AGENT_STATUS: FAILED`;
- `FINAL_STATUS` è riservato a Python e la sua presenza nell'output agent è un errore di protocollo;
- architect `READY` richiede immediatamente prima del marker un unico `ISSUE_REF_JSON` v1 con schema chiuso e identità coerente col locator pre-risolto;
- architect `READY`, ogni `FAILED` e `CHANGES_REQUIRED` richiedono un body non vuoto; coder `COMPLETED` e reviewer `APPROVED` possono avere body vuoto;
- marker duplicati/conflittuali, valori sconosciuti, envelope malformati e trasporto ambiguo producono `PROTOCOL_ERROR` quando non esiste già un outcome tecnico a precedenza superiore.

`CHANGES_REQUIRED` è una risposta valida con outcome tecnico `SUCCEEDED`; soltanto la state machine decide l'eventuale ciclo successivo. Il body resta opaco e viene trasportato integralmente.

La precedence per attempt resta `TIMEOUT → PROVIDER_ERROR trusted → PROCESS_ERROR → PROTOCOL_ERROR → AGENT_REPORTED_FAILURE → SUCCEEDED`. Una violazione di trasporto osservata insieme a un outcome superiore resta diagnostica concorrente e non riclassifica il tentativo né autorizza retry aggiuntivi.

## Conseguenze

- Testo proveniente da issue, tool o stderr non può pilotare la state machine.
- Un cambiamento del trasporto viene assorbito dall'adapter, non dalla grammatica applicativa.
- Il protocollo è intenzionalmente rigido: output quasi valido viene rifiutato invece di essere corretto euristicamente.
- Evoluzioni incompatibili richiedono una nuova versione di protocollo, fixture e migrazione esplicita.

## Alternative scartate

- Parsing di tutto stdout/stderr o ricerca dell'ultima occorrenza di un marker.
- JSON libero o marker racchiusi in code fence.
- Inferenza probabilistica dello status dal testo naturale.
- Condivisione della grammatica del trasporto con il protocollo applicativo.

## Quando riesaminare

Riesaminare se cambiano gli stati di dominio, lo schema issue envelope o il contratto terminale degli agenti. Un mero nuovo formato OpenCode appartiene invece ad ADR-005.

## Tracciabilità

PRD FR-017–FR-019, FR-024, FR-035–FR-038, FR-047–FR-049; AC-016, AC-017, AC-029–AC-031, AC-035; System Design §§9 e 13.2.
