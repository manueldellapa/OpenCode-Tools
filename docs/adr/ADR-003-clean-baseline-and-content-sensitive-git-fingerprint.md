# ADR-003: Clean baseline e fingerprint Git content-sensitive

- **Stato:** Accettato
- **Data:** 2026-09-11
- **Ambito:** OpenCode-Tools v0.1
- **Fonti canoniche:** [PRD](../../tasks/prd-opencode-tools.md), [System Design](../../tasks/system-design-opencode-tools.md)

## Contesto

La pipeline deve attribuire le modifiche al run, distinguere un tentativo sicuro da uno parzialmente mutativo e impedire una falsa approvazione dopo drift Git. Il solo output porcelain non rileva una seconda modifica a un file già marcato `M`; una baseline dirty renderebbe inoltre ambigua l'attribuzione senza un modello di merge o rollback che v0.1 esclude.

## Decisione

Ogni run v0.1 richiede un target:

- contenuto nel workspace dopo la risoluzione dei symlink e coincidente con il top-level Git;
- non bare, su branch nominato, con `HEAD` risolvibile;
- privo di modifiche staged, unstaged e untracked non ignorate;
- senza flag o configurazione che aggirino la clean baseline.

La baseline e ogni checkpoint usano il fingerprint versionato `git-state-v1`: hash SHA-256 di record ordinati e length-prefixed che coprono porcelain raw, index, path tracked e untracked non ignorati, tipo, contenuto o target del symlink ed executable bit Git-significativo. Path non UTF-8 mantengono una rappresentazione byte-safe. Letture instabili vengono ritentate una volta entro la deadline; ambiguità o errori producono stato `INDETERMINATE`, mai clean presunto.

Uno snapshot viene acquisito immediatamente prima e dopo ogni provider attempt, con continuità rispetto all'ultimo checkpoint accettato. Branch e `HEAD` devono restare uguali alla baseline. Un delta di architect o reviewer è `GIT_SAFETY_ERROR`; un delta coder riuscito è l'output atteso, mentre un provider error del coder dopo un delta preserva le modifiche e sopprime il retry. Python non esegue reset, clean, stash, checkout o rollback.

## Conseguenze

- Le modifiche osservate dopo l'avvio sono attribuibili operativamente al run, salvo il rischio dichiarato di processi esterni.
- Il secondo edit di un file già modificato viene rilevato anche se il codice porcelain non cambia.
- Repository grandi pagano il costo dell'hashing; timeout o race falliscono chiuso.
- Dopo una modifica parziale l'utente deve ispezionare e ripristinare consapevolmente una baseline clean prima di una nuova run.

## Alternative scartate

- Baseline dirty abilitata da un semplice flag.
- Confronto basato soltanto su `git status --porcelain`.
- Rollback o stash automatico delle modifiche.
- Attribuzione certa delle modifiche a uno specifico processo locale.

## Quando riesaminare

Il supporto a target dirty richiede nuovo PRD/ADR, schema di baseline, modello di attribuzione e acceptance dedicate. Un nuovo algoritmo di fingerprint incompatibile richiede una nuova versione del framing.

## Tracciabilità

PRD G-004; FR-013–FR-015, FR-020–FR-023, FR-031, FR-039–FR-041, FR-050–FR-051; OQ-010; AC-005, AC-006, AC-013, AC-018, AC-019, AC-028, AC-036; System Design §11.
