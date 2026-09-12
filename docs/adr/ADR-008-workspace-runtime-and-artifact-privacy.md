# ADR-008: Workspace runtime e artifact privacy

- **Stato:** Accettato
- **Data:** 2026-09-11
- **Ambito:** OpenCode-Tools v0.1
- **Fonti canoniche:** [PRD](../../tasks/prd-opencode-tools.md), [System Design](../../tasks/system-design-opencode-tools.md)

## Contesto

Gli artifact devono essere facilmente individuabili in un workspace multi-repository senza sporcare il target e possono contenere path, issue private, codice o secret stampati da processi esterni. Una user cache centrale riduce la discoverability; una runtime nel target frammenta lo stato. Redaction perfetta e retention automatica non sono garantibili nel baseline v0.1.

## Decisione

La runtime predefinita è `<workspace>/.opencode-tools`; `runtime.root` può essere relativo al workspace o assoluto dopo canonicalizzazione. Prima di creare gli artifact di run:

- un path dentro un working tree deve risultare già ignorato, directory e sentinel inclusi, tramite probe fail-closed;
- un `runtime.root` sotto Git metadata è sempre rifiutato; lock e quarantine di coordinamento sotto `<git-dir>` sono disciplinati separatamente da ADR-006;
- il programma non modifica `.gitignore`, mode o ownership per correggere il contesto;
- runtime esistenti devono appartenere all'utente effettivo, non essere symlink e avere tutti i bit group/other azzerati; altrimenti il preflight fallisce senza correggere mode o ownership.

Ogni run usa una directory univoca sotto `runs/`. Directory nuove sono `0700`, file e temp `0600`. `run.json` v1 è l'unica source of truth machine-readable e viene sostituito atomicamente con temp nello stesso filesystem, `fsync` e `os.replace`; i log attempt sono diagnostici append-only e sensibili.

Un errore di apertura o scrittura dei log, oppure di serializzazione, `fsync` o replace di `run.json`, interrompe nuove invocation e produce `LOGGING_ERROR`, exit non-zero e warning su stderr che l'artifact può essere incompleto. Se un child è attivo, una failure del sink avvia la terminazione bounded di ADR-004. La pulizia best effort riguarda soltanto il temp della write fallita; la precedente `run.json` valida non viene sovrascritta.

`run.json` non registra environment completo, credential, raw auth, raw effective OpenCode config, raw sanitized export, prompt nel comando o model/provider ID. I raw agent log possono comunque contenere issue, codice o secret e sono trattati come sensibili anche con mode restrittivi.

V0.1 non applica retention, rotation, prune o cancellazione automatica. L'utente elimina manualmente intere directory di run dopo l'ispezione. Lo storage locale delle sessioni OpenCode è separato e non è governato da questa policy.

## Conseguenze

- Le run sono raccolte in un punto prevedibile per workspace e non vengono sparse nei target.
- Quando workspace e target coincidono, il default richiede `.opencode-tools/` già ignorata oppure un override esterno.
- L'ultima `run.json` valida sopravvive a una sostituzione fallita, ma può essere parziale e viene dichiarata tale.
- Crescita dello spazio, backup e cancellazione restano responsabilità esplicita dell'utente.

## Alternative scartate

- Runtime predefinita nel target o esclusivamente in user cache.
- Path obbligatorio senza default.
- Modifica automatica di `.gitignore`, permission o ownership.
- Redaction regex presentata come garanzia, nessun raw log o cancellazione automatica v0.1.

## Quando riesaminare

Un comando `prune`, retention per età/dimensione, redaction streaming o uno store centralizzato richiedono decisione e acceptance dedicate. Cambi incompatibili a `run.json` richiedono versionamento esplicito.

## Tracciabilità

PRD FR-015, FR-042–FR-046, FR-052; NFR-010; OQ-007, OQ-009; AC-021, AC-022, AC-025, AC-033, AC-034; System Design §§15, 18.3–18.4 e 21.7/21.9.
