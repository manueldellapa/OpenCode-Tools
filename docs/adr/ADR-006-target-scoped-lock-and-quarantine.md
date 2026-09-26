# ADR-006: Target-scoped lock e quarantine

- **Stato:** Accettato
- **Data:** 2026-09-11
- **Ambito:** OpenCode-Tools v0.1
- **Fonti canoniche:** [PRD](../../tasks/prd-opencode-tools.md), [System Design](../../tasks/system-design-opencode-tools.md)

## Contesto

Due run conformi sullo stesso checkout renderebbero impossibile attribuire i cambi e invaliderebbero baseline, review e postflight. Un lock nella runtime configurabile può essere aggirato scegliendo root diverse; un lock per workspace bloccherebbe inutilmente target indipendenti. Dopo una terminazione non confermata, il rilascio del lock OS non prova inoltre che tutti i discendenti siano morti.

## Decisione

SH-002 è promosso nel baseline v0.1. Ogni run acquisisce un lock advisory POSIX non bloccante per target canonico:

- il lock vive sotto il Git directory del checkout, ad esempio `<git-dir>/opencode-tools/target.lock`;
- usa `fcntl.flock(LOCK_EX | LOCK_NB)` e resta detenuto da prima della baseline attraverso postflight, eventuale quarantine e preparazione durabile del candidato terminale; viene rilasciato soltanto quando ogni operazione target-sensitive è conclusa, prima del commit canonico runtime-only di `run.json`;
- due checkout/worktree con Git directory distinte hanno lease distinti; target diversi possono procedere in parallelo;
- directory, lock e metadata usano mode restrittivi; i metadata del nuovo holder sono scritti soltanto dopo l'acquisizione e sono diagnostici;
- la presenza del file non equivale a un lock attivo e non viene applicata alcuna euristica stale basata su PID o tempo;
- un filesystem senza locking advisory affidabile fallisce chiuso.

Se il lease è già detenuto, il contender termina immediatamente con `PREFLIGHT_ERROR/TARGET_LOCKED`, non invoca agenti e mostra i metadata disponibili del holder. La recovery consiste nell'attendere il run attivo; il file non va cancellato come presunto stale.

Se la terminazione di un process group non è confermata, prima di rilasciare il lease viene scritto atomicamente `<git-dir>/opencode-tools/quarantine-v1.json`. La quarantine blocca run successive anche senza lock attivo e non viene rimossa automaticamente. L'utente deve fermare eventuali processi, ispezionare Git e working tree e rimuoverla consapevolmente. Se la quarantine non può essere scritta, il final status resta `FAILED` e stderr avverte che l'esclusione futura non è garantita.

La finalizzazione terminale è a due fasi. Mentre il lease è ancora detenuto, `stage_final` prepara e rende durabile un temp privato ma non modifica la `run.json` canonica. Dopo che postflight e quarantine hanno concluso ogni accesso al target, il lease viene rilasciato e la cancellazione viene osservata ancora una volta; un segnale accettato durante il rilascio aggiorna soltanto il candidato non pubblicato. `seal_cancellation` è quindi il punto di linearizzazione del ciclo di vita, seguito da un solo `commit_final` atomico. In questo modo non serve un secondo overwrite terminale e nessuna operazione target-sensitive avviene dopo il rilascio del lock.

## Conseguenze

- Un solo run conforme può operare su uno specifico checkout; target indipendenti restano paralleli.
- Runtime root differenti non aggirano l'esclusione.
- Un file lock residuo è innocuo, mentre una quarantine residua è intenzionalmente bloccante.
- Crash precedenti alla scrittura della quarantine restano un rischio residuo mitigato dal preflight Git, non eliminato.

## Alternative scartate

- Nessun lock o sola assunzione organizzativa.
- Lock nella runtime o a livello workspace.
- File PID con rimozione automatica per età o mancata esistenza del PID.
- Sblocco automatico della quarantine.

## Quando riesaminare

Riesaminare con filesystem remoti, coordinamento cross-host o supporto Windows. Una strategia distribuita richiede garanzie e acceptance proprie.

## Tracciabilità

PRD SH-002, OQ-004, R-004, R-005; FR-033, FR-039–FR-041; AC-018, AC-032; System Design §§16 e 21.4. Relazioni: ADR-003, ADR-004, ADR-009.
