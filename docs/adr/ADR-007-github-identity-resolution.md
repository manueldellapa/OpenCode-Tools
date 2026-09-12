# ADR-007: GitHub identity resolution senza issue fetch Python

- **Stato:** Accettato
- **Data:** 2026-09-11
- **Ambito:** OpenCode-Tools v0.1
- **Fonti canoniche:** [PRD](../../tasks/prd-opencode-tools.md), [System Design](../../tasks/system-design-opencode-tools.md)

## Contesto

In un workspace multi-repository, cwd e repository del workspace non identificano necessariamente il target. `origin` può mancare, essere non GitHub o divergere da un override. Leggere la issue direttamente in Python sposterebbe inoltre comprensione e trasporto del body nel control plane, in contrasto col confine assegnato all'architect.

## Decisione

Python risolve deterministicamente un `IssueLocator` dal solo target e non legge title/body della issue. La precedenza è:

1. un `repository` target-specifico, quando configurato, definisce l'identità attesa;
2. un `remote` target-specifico limita i fetch URL da considerare;
3. senza remote esplicito, si usa `origin` se normalizza a un'identità GitHub valida;
4. altrimenti si accetta soltanto un'unica identità GitHub distinta fra tutti i remote;
5. override e remote presenti devono coincidere; assenza, molteplicità o mismatch falliscono con `PREFLIGHT_ERROR`.

Sono ammessi URL HTTPS, `ssh://` e scp-like senza credential persistite, query o fragment. Per il confronto, l'host è lower-case, slash finali e suffisso `.git` vengono rimossi e owner/repository usano una forma ASCII case-insensitive conservando lo spelling validato per il display. Sono supportati `github.com` e host GitHub Enterprise per i quali `gh auth status --hostname <host>` supera il preflight.

Il prompt trusted fornisce all'architect numero, target e identità risolta e impone `gh issue view <number> --repo <identity>`. L'architect legge e interpreta la issue e restituisce l'envelope di ADR-002; Python ne verifica coerenza e URL canonico senza una seconda fetch. Python non usa API GitHub native e non esegue mutation GitHub.

## Conseguenze

- La issue non può essere letta accidentalmente dal repository sbagliato.
- Repository senza remote deterministico richiedono un override esplicito.
- GitHub Enterprise è supportabile senza deduzioni dal cwd.
- L'esistenza e il contenuto della issue dipendono dal contratto osservabile dell'architect; Python valida l'identità, non la semantica del body.

## Alternative scartate

- Usare sempre `origin` o il repository del cwd/workspace.
- Scegliere interattivamente fra remote ambigui.
- Fare fetch della issue in Python o introdurre un client API GitHub.
- Accettare mismatch fra override e remote.

## Quando riesaminare

Riesaminare se Python deve acquisire issue metadata/body, se entrano provider diversi da GitHub o se una release introduce mutation GitHub.

## Tracciabilità

PRD G-007; FR-003–FR-005, FR-016–FR-018, FR-022; OQ-006, R-009; AC-003, AC-029; System Design §§17 e 21.6. Relazione: ADR-002.
