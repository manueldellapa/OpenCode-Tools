# ADR-009: POSIX platform baseline

- **Stato:** Accettato
- **Data:** 2026-09-11
- **Ambito:** OpenCode-Tools v0.1
- **Fonti canoniche:** [PRD](../../tasks/prd-opencode-tools.md), [System Design](../../tasks/system-design-opencode-tools.md)

## Contesto

Process-group termination, lock advisory, permission mode e durability del filesystem sono parte delle garanzie di safety, non dettagli portabili automaticamente. Dichiarare supporto Windows best effort senza Job Objects, ACL, locking e test dedicati indebolirebbe i requisiti fail-closed.

## Decisione

V0.1 supporta macOS e Linux su filesystem locale POSIX, con Python 3.13+, Git, GitHub CLI e una versione OpenCode qualificata nel PATH.

Il baseline supportato comprende:

- nuove sessioni/process group, `SIGTERM`/`SIGKILL` e `killpg`;
- `fcntl.flock` non bloccante;
- mode `0700/0600`, primitive anti-symlink e `fsync` della directory quando supportato;
- argv nativi, testo persistito UTF-8 e rappresentazione byte-safe dei path Git raw.

Windows native e WSL su filesystem montati da Windows non sono supportati. WSL su filesystem Linux può essere sperimentale, ma non soddisfa automaticamente gli acceptance v0.1.

Le porte `ProcessRunner`, `TargetLock` e filesystem mantengono isolata la dipendenza dalla piattaforma. Il futuro supporto Windows richiede un nuovo ADR, adapter platform-specific, nuovi acceptance criteria e una matrice di test/smoke; non viene aggiunto tramite branch condizionali sparsi.

## Conseguenze

- La matrice iniziale è più piccola e le garanzie di timeout, lock e privacy hanno semantica verificabile.
- Il package rifiuta ambienti fuori baseline invece di degradare silenziosamente.
- La portabilità futura resta possibile, ma non viene promessa prima delle relative prove.

## Alternative scartate

- Windows best effort con feature mancanti.
- Astrazione Windows completa già in v0.1.
- Supporto del solo Linux, escludendo macOS senza necessità tecnica.

## Quando riesaminare

Riesaminare per Windows, filesystem remoti/non POSIX o ambienti containerizzati che cambiano semantica di processi, lock, mode o durability.

## Tracciabilità

PRD NFR-001, NFR-005, NFR-008, NFR-010; OQ-005; AC-014, AC-032, AC-034; System Design §§19 e 21.5. Relazioni: ADR-004, ADR-006, ADR-008.
