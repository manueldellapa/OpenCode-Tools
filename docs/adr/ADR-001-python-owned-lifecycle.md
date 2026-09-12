# ADR-001: Python-owned lifecycle, nessun orchestrator agent

- **Stato:** Accettato
- **Data:** 2026-09-11
- **Ambito:** OpenCode-Tools v0.1
- **Fonti canoniche:** [PRD](../../tasks/prd-opencode-tools.md), [System Design](../../tasks/system-design-opencode-tools.md)

## Contesto

La pipeline deve combinare reasoning non deterministico degli agenti con transizioni, retry, timeout e finalizzazione verificabili. Distribuire il controllo del lifecycle fra Python e gli agenti introdurrebbe due autorità concorrenti e renderebbe ambigui contatori, failure path e stato finale.

## Decisione

Python è l'unica autorità sul lifecycle della pipeline e:

- invoca direttamente i soli agenti primari `architect`, `coder` e `reviewer`;
- applica state machine, review cycle, provider attempt, retry, timeout, Git gate, postflight e finalizzazione;
- tratta handoff, feedback e spiegazioni come payload opachi, salvo l'envelope e i marker definiti da ADR-002;
- emette l'unico `FINAL_STATUS` e determina l'exit code;
- usa `cli.py` come composition root e mantiene dominio, transizioni e policy pure separate dagli adapter I/O.

Gli agenti comprendono la issue, pianificano, modificano, testano e revisionano. Non esiste un agent OpenCode `orchestrator`, gli agenti non delegano la state machine e ogni invocation crea una sessione indipendente collegata alle altre da handoff espliciti.

## Conseguenze

- Le transizioni e i terminal path sono testabili senza provider o agenti reali.
- Il comportamento non dipende dalla capacità di un modello di ricordare o applicare il lifecycle.
- Python deve modellare esplicitamente stati, eventi e precedenze, ma non incorpora reasoning specifico della issue.
- Cambiare modello o provider di un ruolo non modifica l'orchestrazione.

## Alternative scartate

- **Quarto agent orchestrator:** duplica l'autorità e rende il final status non deterministico.
- **Lifecycle condiviso fra Python e agenti:** non offre una fonte unica per contatori e recovery.
- **Sessione OpenCode persistente come stato della pipeline:** accoppia il lifecycle a uno storage esterno e rende più debole la riproducibilità.

## Quando riesaminare

Un nuovo ADR è necessario se una release introduce orchestrazione multi-issue, delega delle transizioni agli agenti o sessioni persistenti come fonte di stato.

## Tracciabilità

PRD G-002, G-005, G-008; FR-010, FR-016, FR-019, FR-024–FR-027, FR-047–FR-049; System Design §§5, 8 e 23.4.
