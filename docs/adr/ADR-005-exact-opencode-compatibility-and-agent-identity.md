# ADR-005: OpenCode exact-version compatibility e agent identity proof

- **Stato:** Accettato
- **Data:** 2026-09-11
- **Ambito:** OpenCode-Tools v0.1
- **Fonti canoniche:** [PRD](../../tasks/prd-opencode-tools.md), [System Design](../../tasks/system-design-opencode-tools.md)

## Contesto

Il formato degli eventi OpenCode è version-sensitive e la CLI può sostituire silenziosamente un agent richiesto con quello predefinito. Controllare l'exit code, l'argomento `--agent` o una versione semver approssimativa non dimostra né la compatibilità del trasporto né il ruolo realmente eseguito.

## Decisione

La v0.1 sviluppa un adapter per la sola versione esatta OpenCode `1.17.18`. Questo ADR accetta la policy di compatibilità, non certifica che l'evidenza d'implementazione esista già: la release può inserire `1.17.18` nella registry delle versioni supportate soltanto dopo fixture complete e smoke disposable superato. Il preflight di una build rilasciata accetta esclusivamente le versioni presenti in tale registry.

Per una versione qualificata, il preflight:

- registra `opencode --version` una sola volta e seleziona l'adapter per match esatto;
- verifica le capability `run --agent`, `--format json` e `--dir`;
- valida `debug config` e `debug agent <role>` per i tre agenti primari, incluso il baseline di permessi e l'assenza di auto-share, `ask` e `task`;
- rifiuta output o effective policy ambigui e qualunque fallback.

Il preflight calcola inoltre un digest canonico dell'effective config dei tre agenti e della configurazione OpenCode rilevante. Prima di ogni `opencode run` un recheck bounded deve ottenere lo stesso digest; ogni variazione nel run produce `PROTOCOL_ERROR` con codice `OPENCODE_CONTROL_PLANE_DRIFT` prima del ruolo successivo.

Per ogni invocation l'adapter valida l'NDJSON rispetto alla fixture e richiede un unico session ID. Dopo ogni `run` tenta `opencode export <session-id> --sanitize`, ne valida lo schema in memoria e richiede che ogni messaggio assistant top-level riporti in `info.agent` il ruolo richiesto. Una violazione è `PROTOCOL_ERROR`; se coesiste con `TIMEOUT`, `PROVIDER_ERROR` trusted o `PROCESS_ERROR`, resta diagnostica concorrente secondo la precedence di ADR-002. Raw debug/export e model/provider ID non vengono persistiti; resta la prova tramite ruolo verificato, metodo, versione e digest.

Una nuova versione richiede adapter/schema dedicato, fixture immutabili con provenance per success, failure, provider signature, malformed stream, fallback e identity, capability check aggiornato e smoke documentato. Non esistono fallback a testo libero, agent default, versione sconosciuta o classificazione best effort.

## Conseguenze

- Compatibilità e agent identity sono evidenze versionate, non assunzioni.
- Gli upgrade OpenCode richiedono lavoro esplicito e possono bloccare il preflight.
- Export e recheck aggiungono processi utility per attempt, ma impediscono transizioni dopo fallback non rilevato.
- I model ID restano esterni al codice Python e possono cambiare fra run.

## Alternative scartate

- Supportare `latest` o un range semver senza fixture per ogni versione.
- Fidarsi di `--agent`, exit code o auto-attestazione nel marker.
- Usare warning testuali o stderr non versionati come prova.
- Continuare in modalità best effort su schema sconosciuto.

## Quando riesaminare

Se le fixture o lo smoke non dimostrano la prova debug/export per `1.17.18`, la versione resta non supportata e questo ADR deve essere rivisto; non si introduce un fallback per superare il gate.

## Tracciabilità

PRD FR-009, FR-011–FR-012, FR-021–FR-022, FR-028, FR-035; NFR-008–NFR-009; OQ-002, OQ-008; AC-007, AC-023, AC-024, AC-027, AC-031, AC-035; System Design §§10.4–10.6, 18.2, 20.4–20.5 e 21.2/21.8.
