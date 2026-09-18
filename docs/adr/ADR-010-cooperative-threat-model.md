# ADR-010: Threat model cooperativo e future containment

- **Stato:** Accettato, emendato da issue #90
- **Data:** 2026-09-11
- **Ambito:** OpenCode-Tools v0.1
- **Fonti canoniche:** [PRD](../../tasks/prd-opencode-tools.md), [System Design](../../tasks/system-design-opencode-tools.md)

## Contesto

Permission OpenCode, prompt e controlli Git riducono errori e prompt injection, ma non costituiscono una sandbox contro processi locali deliberatamente ostili. Presentare tali controlli come contenimento forte creerebbe una garanzia falsa; introdurre subito una sandbox multipiattaforma allargherebbe scope e acceptance della v0.1.

## Decisione

V0.1 adotta un threat model cooperativo e fail-closed, con una garanzia
più forte e circoscritta introdotta da issue #90: il coder non opera più
sui metadati Git reali del target.

Sono input non fidati issue body, contenuto del repository, filename, handoff/feedback, stream di processo ed errori provider. Sono control plane fidato il codice installato OpenCode-Tools, la configurazione validata, adapter versionati, transition table e definizioni/effective config agent verificate. Git, `gh` e `opencode` locali sono dipendenze operative assunte non compromesse.

I controlli v0.1 mitigano errori, output malformato, prompt injection in un contesto cooperativo, fallback agent, concorrenza fra run conformi e mutation Git osservabile tramite:

- argv strutturati, prompt su stdin e separazione dei canali trusted;
- permission matrix verificata, nessun share e control-plane digest immutabile durante il run;
- protocollo e compatibility proof fail-closed di ADR-002/ADR-005;
- clean baseline, fingerprint, lock, postflight e artifact privati di ADR-003/ADR-006/ADR-008;
- per ogni attempt coder, clone locale disposable con object store indipendente, nessun remote e nessun alternate object store; il coder riceve solo quel clone come `--dir`;
- prima della promozione Python verifica `.git`, top-level e `HEAD` della sandbox, ricontrolla il fingerprint del target reale e applica esclusivamente il working-tree delta attraverso comandi Git trusted.

La sandbox coder di issue #90 protegge specificamente il repository Git
reale da cancellazione/reinizializzazione dei suoi metadati e da mutation
Git effettuata nel clone disposable. Non è una sandbox OS generale e non
promette contenimento contro un processo con gli stessi permessi utente che
riesca a uscire dal boundary OpenCode, un binary locale compromesso,
credential misuse o mutation remota effettuata tramite credenziali esterne.

Dopo v0.1, eventuali misure ulteriori vengono valutate in ordine:
minimizzazione credenziali/command broker e sandbox OS più generale.
Ogni garanzia aggiuntiva richiede aggiornamento di PRD, threat model e
acceptance.

## Conseguenze

- I controlli correnti sono testabili e le loro lacune sono esplicite.
- Un agent cooperativo può comunque restare semanticamente influenzato da contenuto malevolo della issue o del repository.
- L'utente deve scegliere provider e policy compatibili con repository privati e resta responsabile delle credential locali.
- La distruzione di `.git`, `git init`, reset/clean/ref rewrite nel clone coder non può più distruggere i metadati Git del target reale; una sandbox corrotta blocca la promozione.
- Il rischio residuo di escape dal boundary OpenCode e mutation remota tramite altri canali resta dichiarato.
- Nessuna garanzia OS-level ulteriore entra implicitamente nell'implementazione corrente.

## Alternative scartate

- Dichiarare permission policy e Git checks equivalenti a una sandbox.
- Container, utente OS dedicato o worktree obbligatori nel baseline v0.1.
- Nessun controllo cooperativo in attesa di un contenimento forte futuro.

## Quando riesaminare

Riesaminare prima di promettere isolamento ostile, ridurre il rischio residuo R-001/R-006 o cambiare il contratto di output non committato nel target corrente.

## Tracciabilità

PRD FR-018, FR-020–FR-022; NFR-008, NFR-010; OQ-003, R-001, R-002, R-006, R-011; AC-024, AC-031, AC-034, AC-035; System Design §§11.6, 18 e 21.3.
