# Athenaeum — Lessons Learned

> **Learnings live in the Athenaeum library.** Query at session start via
> `request_knowledge` (e.g. "project lessons", "phase 5 lessons"); persist new
> learnings via `store_knowledge` (kind_hint `lessons`, relate to the relevant
> phase/version concepts). Migrated from this file on 2026-07-29.
>
> What stays local in this file:
>
> 1. **Dogfooding findings** — observations about Athenaeum's own behavior;
>    they feed the fix list and must not depend on the system they critique.
> 2. **Bootstrap-critical notes** — what you need when the Athenaeum MCP
>    server is down or the library is broken.

## Bootstrap-critical notes

- FastMCP Streamable HTTP session manager must be driven by the FastAPI
  `lifespan=` context manager — never startup/shutdown decorators (verified
  against fastmcp 3.4.5, `StarletteWithLifespan`).
- Every mutation is a fixed-order compound write: concept file
  (write-then-rename, `.tmp` + fsync + atomic rename) → regenerate affected
  `index.md` → append root `log.md` → git auto-commit (best-effort, never
  breaks the write). Index/log are automatic side effects, never LLM-callable.
- PD-1: new settings default to Admin WebUI + database, never env vars.
- When the external MCP surface changes, update `architecture.md`,
  `project-definition.md`, `README.md`, and `AGENTS.md` in the same change.
- Kilo MCP clients see new/changed tools only after a session restart (tool
  catalog is read at session start); for ad-hoc tests use a raw JSON-RPC call
  (initialize → tools/call) against `/mcp`.
- MCP sessions and redeploys (2026-08-28, REMOTE): stateful mode (default)
  keeps sessions in server RAM — a container restart kills them, and the MCP
  TypeScript SDK has no 404 re-initialize, so harness sessions stay dead
  (F19 side finding, also observed under DeepSeek Harness). Durable fix:
  `mcp_stateless_http` (Admin > Server, takes effect on restart) — stateless
  mode survives redeploys with no client-side restart; verified live.

## Dogfooding findings

> Findings from real MCP usage, recorded per AGENTS.md; feeds the phase-2
> fix list. F1–F4 and F6 were resolved in 0.4.0 via the
> `DEFAULT_SYSTEM_PROMPT` revision (+ backend fix for F3); F5 is intended
> behavior.

- **F1 — Retrieval missed cross-references.** A query about "Phase 3"
  answered "not in the library" although `/versions/athenaeum-mcp-v0.3.0.md`
  documents exactly that phase; the librarian matched literal tokens
  instead of following links. → Prompt now requires link-following and
  synonym/alias retries before concluding absence.
- **F2 — Hollow sections.** The server concept got a "## Tools Available"
  section containing only "All 5 tools are operational" — no list, although
  the librarian had the data. → New write-discipline rule "NO EMPTY
  SECTIONS" (contract-pinned in `test_prompts.py`).
- **F3 — `generated.at` (FEHLANALYSE, korrigiert in 0.4.2).** Ursprüngliches
  Finding: „`generated.at` wird bei Update refresht, Creation-Zeit geht
  verloren" → 0.4.0 machte `generated` zur Creation-Provenance. **Falsch:**
  SPEC §5.2 definiert `generated.at` explizit als „last meaningful change"
  — ein Freshness-Signal für Consumer, kein Creation-Feld (OKF v0.2 hat
  schlicht keins). Der 0.4.0-Fix brach damit die Spec. 0.4.2 revidiert:
  Edit/Deprecate refreshen `generated.{by,at}` wieder. **Learning:**
  Dogfooding-Beobachtungen gegen die autoritative Spec prüfen, bevor man
  sie als Bug klassifiziert — die lokale reference.md sagte es richtig
  („at is an ISO 8601 datetime of the last meaningful content change"),
  die Fehlinterpretation entstand im Review.
- **F8 — `is_stale` Off-by-one (RESOLVED in 0.4.2).** Spec §5.5: stale
  *on/after* `stale_after` (`today >= stale_after`); der Code nutzte strikte
  `<`-Vergleiche — am Stichtag selbst „nicht stale". Gefunden beim
  Spec-Re-Review nach dem F3-Schock. Learning: bei Datumsvergleichen die
  Spec-Formulierung („on/after") wörtlich nehmen.
- **F9 — Bulk-Umstrukturierungen knacken das Iterations-Cap (Beobachtung).**
  Die 7-Move-Umstrukturierung (0.4.3) endete nach 5 Moves am
  `max_iterations=10`-Cap; der Librarian kommunizierte die ausstehenden
  Moves sauber und ein Folge-Request schloss sie ab. Kein Defekt, aber für
  Bulk-Tasks ggf. Cap erhöhen oder Moves bündeln (ein Tool-Call pro Move
  kostet eine Iteration).
  **Korrektur/Aufloesung (2026-08-19, RESOLVED):** Die Praemisse "ein
  Tool-Call = eine Iteration" ist ueberholt — eine Iteration zaehlt pro
  Assistant-**Response**, und eine Response kann mehrere Tool-Calls
  tragen (einmal gezaehlt; test-gepinnt in test_librarian_loop.py).
  Zudem ist `max_iterations` laengst ein PD-1-konformes Setting pro
  Provider-Connection (Default 10, WebUI/DB). Fix: neue Prompt-Regel
  "BUNDLE INDEPENDENT CALLS" in der geteilten Retrieval-Sektion (gilt
  fuer Librarian- und Curator-Prompt, Vertrags-Pin in test_prompts.py) —
  das Modell soll unabhaengige Calls in einer Response buendeln statt
  einzeln zu troepfeln. Cap-Erhoehung und Batch-Move-Tool evidenzbasiert
  verworfen (Kosten-Airbag aus F12/F15 bleibt; Scheduler-Timeout koppelt
  ans Cap).
- **F10 — Reasoning-Leakage in Final-Answers (MITIGIERT in 0.4.4).**
  Verdacht war ein fehlender Reasoning-Filter; der Debug-Call (rohes JSON von
  OpenRouter, Modell gpt-oss-120b:nitro) zeigte: Reasoning wird sauber in
  `message.reasoning`/`reasoning_details` separiert (282 Reasoning-Tokens,
  separat abgerechnet), `content` ist sauber, und unser Adapter liest ohnehin
  nur `content`. Kein Filter-Bug. Die geleakten Prozess-Fragmente („We
  haven't read it yet, but we can approximate…") wurden vom Modell **als
  `content` geschrieben** — Modellverhalten nach Erreichen des
  Iterations-Caps (Final-Answer ohne Tools). Milderung in 0.4.4: neue
  Kontrakt-Regel „ANSWER HYGIENE" im Prompt + verschärfter
  `FINAL_ANSWER_REQUEST` (nur Ergebnisse/Zitate, Lücken als plain coverage
  gaps). Nebenbefund: `reasoning.exclude: true` entfernt das Reasoning-Feld
  aus der Response, spart aber keine Token-Kosten.
- **F11 — Stiller Store-No-op (RESOLVED in 0.7.1).** Zwei
  `store_knowledge`-Aufrufe (0.7.0-Deployment-Kontext) endeten mit
  `outcome=ok`, aber `stored: []` und leerer Summary. Trace zeigte: 5
  Read-Calls (list/search/read), dann beendete gpt-oss den Loop mit
  **leerer Textantwort** ohne je `create_concept` aufzurufen — dieselbe
  F10-Mechanik (Antwort landet im Reasoning-Kanal, `content` leer). Fix:
  `_run_write_task` erkennt die Signatur (0 Writes + leere Summary),
  retried einmal mit Nudge, sonst `LibrarianNoWriteError` → `ToolError`;
  neue Kontrakt-Regel „A STORE ENDS IN WRITES". Learning: „ok"-Outcome
  allein sagt nichts über den Schreiberfolg — die Write-Intention muss
  deterministisch verifiziert werden, nicht dem Modell überlassen.
- **F12 — Gestapelte Fehlerursachen (RESOLVED in 0.7.1-0.7.3).** Der
  0.7.0-Store scheiterte viermal mit vier verschiedenen, sich
  überlagernden Ursachen — jede Fix-Schicht deckte die nächste auf:
  (1) leere Final-Answer nach Cap (F11-Retry + ToolError, 0.7.1);
  (2) redundante Re-Reads verbrannten das Iterations-Budget
  (NEVER-RE-READ-Regel, 0.7.2); (3) `write_concept` ohne `body`-Argument
  crashte mit KeyError statt recoverablem Fehler (Dispatch-Validierung,
  0.7.3) — erst dann gelang der Store. Nebenbefund: das Modell setzte
  dabei einen Backlink auf ein nicht existierendes Konzept (OKF-toleriert,
  wurde durch den erfolgreichen Store geheilt). Learnings: bei
  „unerklärlichen" LLM-Fehlschlägen lohnt der Blick in den Trace vor jeder
  Hypothese; Fehlermeldungen an das Modell müssen **recoverable formuliert**
  sein („missing required argument(s): body"), nicht rohe Exceptions —
  das Modell ist ein Consumer der Tool-Fehler.
- **F13 — Cap-Exit mit nicht-leerer Summary umgeht die F11-Guard
  (2026-07-29, RESOLVED in Deep-Review Phase 1).** Beim Migrieren der lessons.md in die Library
  endete ein `store_knowledge`-Call mit `stored: []`, aber **nicht-leerer**
  Summary („tool-use limit reached … not persisted … coverage gap"). Die
  F11-Signatur (0 Writes + leere Summary) griff nicht, weil das Modell beim
  Cap-Exit eine ehrliche Text-Summary schrieb statt einer leeren. Ergebnis:
  `outcome=ok`, nichts gespeichert, kein Retry, kein `ToolError` — der
  Aufrufer muss `stored: []` selbst interpretieren. Aufloesung (Phase 1):
  `_run_write_task` akzeptiert einen Lauf nur noch bei mindestens einem
  gelandeten Write — eine nicht-leere Summary ohne Writes (inkl. Cap-Exit)
  ist jetzt ein expliziter `LibrarianNoWriteError` nach einem Nudge-Retry.
  Learning: die No-Write-Erkennung muss auf `stored == []` allein basieren
  (unabhaengig vom Summary-Inhalt).
- **F14 — Near-Duplicate-Scan merged Namensserien (2026-07-30, RESOLVED in 0.11.1).**
  Erster Curate-Lauf nach der Lessons-Migration: der Titel-Token-Jaccard
  (Schwelle 0.6) stufte alle `phaseN-lessons`-Konzepte als Duplikate ein
  (shared tokens „athenaeum/lessons/phase" — 10 Paare, alle genau 0.6).
  Der Curator mergte daraufhin phase3/4/4c/5-lessons in phase1-lessons
  und deprecated die Originale — dabei sind die Dokumente bewusst
  phasengetrennt. Nebenbefund: die Deprecations verwaisten v0.8.0/v0.9.0
  (Backlinks entfernt), `library_status` meldete danach 3 Orphans +
  12 Warnings. Learnings: (1) Der Ähnlichkeitsscan braucht
  Serien-Awareness — Dokumente, die sich nur in Versions-/Phasen-Token
  unterscheiden (phase3 vs phase4), sind KEINE Duplikate; Kandidaten:
  numerische/Versions-Tokens vom Jaccard ausschließen, Schwelle fuer
  Titel-only-Serien erhoehen, oder Inhalts-Tokens einbeziehen.
   (2) Die Curator-Prompt-Regeln sollten „never merge documents that
   differ only in a version/phase identifier" pinnen. (3) Merge-Aktionen
   des Curators koennen Backlink-Verwaister erzeugen — Maintain-Lauf
   danach pruefen. Aufloesung (0.11.1): `_is_series_pair` im Scan (Paare,
   die sich nur in numerischen Tokens unterscheiden, werden uebersprungen),
    Curator-Prompt-Pin gegen Serien-Merges, `health_after` (healthy +
    Orphan-Anzahl) im Curate-Result. Nachklapp (0.11.2): der Live-Re-Test
    zeigte eine Lücke — `4c`-artige Tokens (Ziffern+Buchstabe) matchten
    `\d+` nicht, phase4c-Paare blieben flagged; `_NUMERIC_RE` auf
    `\d+[a-z]*` erweitert. Learning: Versions-/Phasen-Schema des eigenen
    Projekts (4b, 4c!) beim Regexp-Design mitdenken, und der erste
    Live-Test nach einem Fix gehoert zum Fix, nicht danach.
- **F14-Restore (2026-07-30, Beobachtungen).** Wiederherstellung ueber
  `update_knowledge` in 4 Calls: (a) Der Gesamt-Restore (9 Konzepte)
  endete wieder im F13-Muster — `stored: []` + nicht-leerer Summary mit
  sauberem Arbeitsplan statt Writes; Aufsplittung in 3 kleine Calls
  funktionierte sofort. (b) In einer Summary leakte wieder ein
  Reasoning-Fragment („We need to read it." — F10-Mechanik, trotz
  ANSWER-HYGIENE-Regel). (c) Endzustand nach Restore: `library_status`
  healthy, 0 Orphans, 0 broken links. Restore ueber den Librarian ist
  machbar, aber nur in kleinen Happen zuverlaessig.
- **F15 — Vector Search Live-Verifikation (0.12.0, 2026-07-30, RESOLVED in 0.13.0).** Erster
  echter `request_knowledge`-Call mit aktivierten lokalen Embeddings
  (all-MiniLM-L6-v2): Trace `20260730T125817Z-3673fed6` zeigt
  `search_semantic` als Primaer-Tool (6/10 Hops, je ~18-27 ms lokal),
  Fallback-Disziplin intakt (2x `search_metadata` beigemischt), Backfill
  via Reconcile (19 Konzepte), Tracing-Shape `{path, score}` korrekt.
  Drei Befunde: (a) **Score-Verteilung ist modellabhaengig** — MiniLM
  lieferte Top-Scores 0.23-0.39 (deutsche Query, gemischtsprachige Docs);
  die Curate-Duplikatschwelle 0.85 wurde fuer BGE kalibriert und duerfte
  fuer MiniLM zu strikt sein — bei Modellwechsel Kalibrierung pruefen
  (Schwelle ist TUNABLE in library/semantic.py). (b) **Antwort-Ungenauigkeit:**
  die Antwort behauptete, der Index werde von sqlite-vec verwaltet — das ist
  nur der dokumentierte >10k-Upgrade-Pfad; Modell hat den Konzepttext
  ueberlesen. (c) **Semantische Re-Queries brennen Iterationen:** 4 der 6
  semantic-Calls waren nahezu identische Umformulierungen (Synonym-Retry-
  Muster auf neuer Ebene); Lauf endete am Cap (10 Iterationen, 77k Tokens).
  Ggf. Prompt-Regel "rephrase nur mit neuem Vokabular, nicht bei Treffer"
  ergaenzen. Aufloesung (0.13.0): (a) Per-Modell-Schwellen in
  library/semantic.py (MiniLM 0.80, aus der Live-Messung kalibriert;
  unbekannte Modelle fallen konservativ auf 0.85 zurueck) plus optionalem
  Per-User-Override auf dem Embeddings-Tab (nullable
  `semantic_threshold`-Spalte auf `librarian_configs`; leer = Modell-Default,
  Override gewinnt — PD-1: DB + WebUI, kein Env). (b) wurde am 2026-07-30
  ueber eine Konzepttext-Korrektur per MCP gefixt (keine Code-Aenderung
  noetig). (c) Prompt-Regel im Retrieval-Disziplin-Abschnitt — ein
  `search_semantic`-Call pro Informationsbedarf, Umformulierung nur mit echt
  neuem Vokabular oder anderem Aspekt — mit Vertrags-Pin in
  tests/test_prompts.py.
- **F16 — Fehler nach Teil-Write: Retry-unsicheres Tool-Verhalten (2026-07-30, RESOLVED in Deep-Review Phase 1).**
  Ein `update_knowledge`-Lauf schlug mit "Rate limited by upstream API" fehl
  (Cerebras 429 mitten im Loop), hatte den eigentlichen Edit aber bereits in
  einem frueheren Hop erfolgreich geschrieben. Der naechste Retry meldete dann
  korrekt "already present, no edit required". Fuer den Caller sieht das wie
  ein kompletter Fehlschlag aus, obwohl der Write gelandet ist — Retries sind
  dadurch nicht idempotent aus Caller-Sicht. Aufloesung (Phase 1): ein
  Provider-Fehler mitten im Loop wird zu einem Partial-Success-Result —
  `stored` enthaelt die gelandeten Writes, `partial: true` markiert den
  Abbruch, die Summary benennt die Unterbrechung, und `sync_embeddings`
  laeuft fuer die gelandeten Writes trotzdem. Ohne gelandete Writes
  propagiert der Fehler unveraendert (jetzt als sanitisierter generischer
  ToolError, siehe CS-5).
- **F17 — Fremde Lessons landeten in `/athenaeum/` (2026-07-30, RESOLVED in 0.14.0).**
  Ein `store_knowledge`-Call mit HA-AgentHub-Lessons (kind_hint `lessons`)
  legte die Konzepte unter `/athenaeum/` ab statt einen eigenen
  Top-Level-Bereich zu schaffen. Ursache: die Placement-Regel ("prefer
  extending an existing area over minting a new top-level folder") enthielt
  keinen Subjekt-Test — die Dokumentart (lessons) wurde faktisch zum
  Platzierungskriterium, weil `/athenaeum/` bereits Lessons-Konzepte
  enthielt. Aufloesung (0.14.0): Subjekt-Match-Regel im Prompt
  (kontrakt-gepinnt: "NAME THE SUBJECT FIRST"; "kind is NOT a topic"),
  STORE-TASK-Klarstellungen (related concepts = back-link candidates, NOT
  placement hints; Subjekt + Ziel-Topic-Area vor dem ersten Write nennen)
  und neuer optionaler MCP-Parameter `topic_hint`, der die Ziel-Area direkt
  benennt. Learning: Placement-Regeln brauchen ein positives
  Match-Kriterium (Subjekt), nicht nur eine Negativliste (keine
  Typ-Ordner).
- **F18 — Maintain-Summary zeigt Pre-Run-Findings als "remaining" (2026-07-30, RESOLVED in Deep-Review Phase 1).**
  Beim Library-Cleanup (F17-Nachbereitung): ein Maintain-Lauf repairte
  Orphans korrekt per Backlink, meldete aber in derselben Summary genau
  diese Konzepte als "Remaining coverage gaps (orphans...)" — der
  Summary-Text wiederholt die Eingangs-Findings statt den Endzustand
  erneut zu scannen. Direktes `library_status` danach zeigte die Orphans
  als gefixt. Aufloesung (Phase 1): Maintain- und Curate-Summaries enden
  jetzt mit einer deterministischen "Post-run check"-Zeile aus dem
  Post-Run-Rescan, und das Curate-Result liefert `findings` aus demselben
  Post-Run-Scan wie `organized`/`health_after` (eine Epoche pro Response).
  Learning: Maintain/Curate-Summaries sind kein
  Verifikationsersatz — Endzustand immer per `library_status` pruefen. Nebenbefund:
  Bulk-Move von 6 Konzepten via `update_knowledge` (ein Call, eine
  praesize Relocation-Instruktion mit vollstaendiger Dateiliste) lief in
  einem Lauf durch — Kontrast zu F9 (7 Moves am Cap): einheitliche
  Move-Only-Tasks sind iterationsschonend.
- **OKF-Namedropping raus aus dem Prompt (0.4.4).** Modelle kennen OKF v0.2
  nicht und müssen es auch nicht — sie brauchen nur die Regeln. Der Verweis
  lud zu halluziniertem „Spec-Wissen" ein; die Strukturregeln stehen jetzt
  eigenständig im Prompt. Generelles Prompt-Prinzip: nur deklarieren, was
  das Modell befolgen muss; keine externen Referenzen ohne Nutzen.
- **Taxonomie (0.4.3).** Die Spec überlässt Ordnerstruktur dem Producer —
  ohne Prompt-Guidance entstehen typ-basierte Top-Level-Ordner
  (`projects/`, `servers/`, …) aus kind_hints. Guidance „organize by topic,
  never by document type" + Empty-Dir-Pruning in `move_concept`/
  `delete_concept` eingeführt; Live-Umstrukturierung verlief verlustfrei
  (Link-Rewrite über 7 Moves, keine broken links).
- **Live-Verifikation Curate (0.5.0, 2026-07-29).** Erster `library_curate`-
  Lauf nach der 0.4.3-Umstrukturierung: voller Scan (`since=null`,
  7 Konzepte), leere Findings, No-op **ohne LLM-Call** (Journal:
  `iterations=None, tokens=None, 0.0s`), keine Trace-Datei — D6-Konvergenz
  live bestätigt. Zweiter Lauf nach einem Store: `since` gesetzt,
  inkrementeller Scope aktiv, erneut No-op. Hinweis: Kilo-MCP-Clients sehen
  neue Tools erst nach Session-Neustart (Tool-Katalog wird beim
  Session-Start gelesen); für Ad-hoc-Tests half ein roher JSON-RPC-Call
  (initialize → tools/call) gegen `/mcp`.
- **F4 — Intent mixing.** A retrieval answer offered to *write* placeholder
  concepts. → Prompt: retrieval answers only, never offer writes.
- **F5 — Enrichment churn (intended).** Each store triggers back-link
  updates of related concepts (visible as create→update cascades in
  `log.md`). Kept; now visible via traces.
- **F6 — Vocabulary drift.** Answers conflated frontmatter `status`
  (draft|stable|deprecated) with the tool's `trust_tier`
  (unverified|machine-confirmed|human-reviewed). → Prompt pins exact
  vocabularies.
- **F7 — OpenAI-Adapter crasht auf 200-ohne-`choices` (RESOLVED in 0.4.1).**
  OpenRouter lieferte HTTP 200 mit einem JSON-Body ohne `choices`
  (Fehler-Payload); der Adapter machte nach `raise_for_status()` direkt
  `data["choices"][0]` → `KeyError: 'choices'`, der Librarian-Loop brach
  ab; Retries liefen in MCP-Timeouts. Fix: neuer `LLMProviderError`;
  OpenAI-Adapter wirft ihn mit `error.message` bei 200-mit-`error`-Body
  oder fehlenden `choices`; Anthropic bei `type: "error"`; Gemini bei
  `promptFeedback.blockReason` (vorher: stille leere Antwort). Merke:
  `raise_for_status()` reicht bei LLM-Gateways nicht — Fehler-Payloads
  können mit HTTP 200 kommen; Response-Struktur immer defensiv prüfen.
  Für Phase 6 (LLM-Fallback): `LLMProviderError` und HTTP-Fehler sind die
  Fallback-Auslöser; Modell-Refusals (leere Textantwort) nicht.
- **Persistenz-Lücke (geschlossen 2026-07-28):** Die Phase-4-Completion konnte
  wegen F7 zunächst nicht in die Bibliothek geschrieben werden. Nach dem
  0.4.1-Deployment (Container-Rebuild) wurde sie via `store_knowledge`
  nachgetragen: `/projects/athenaeum-phase4-librarian-transparency`,
  `/versions/athenaeum-mcp-v0.4.0`, `/versions/athenaeum-mcp-v0.4.1` +
  Backlink-Updates. Der Live-Lauf validierte Phase 4 end-to-end: Trace-Datei
  mit 16 Events + LLM-Metadaten (10 Iterationen, 67830 Tokens) und
  Journal-Zeile mit korrelierender Trace-ID.
- **Live-Beobachtung:** Der Store erzeugte 3 neue Konzepte + 2 Backlink-
  Updates in 10 Loop-Iterationen (~29 s, 68k Tokens via openrouter). Für
  Phase 5/6 beachten: Enrichment-Kaskaden treiben Iterationszahl und
  Tokenverbrauch spürbar.
- **Deep Code Review 2026-07 (RESOLVED in 0.15.0).** 51 Befunde
  (kritisch/hoch/mittel/niedrig) in 4 Phasen gefixt; Details in
  `docs/SubAgent/DEEP_REVIEW/CHANGES.md` und VERSION.md 0.15.0. Zentrales
  Learning: die Phase-3-Vereinheitlichung von `is_stale` auf `<` hat die
  F8-Grenzsemantik (Spec §5.5: stale am Stichtag selbst, `<=`) zunächst
  **regressiert** — erst im Merge-Review aufgefallen, weil Plan-Text und
  Spec widersprüchlich waren. Konsequenz: spec-gepinnte Verhaltensweisen
  brauchen einen Contract-Test, der jetzt existiert
  (`test_is_stale_boundary_stale_on_the_day_itself`). Zweites Learning:
  die No-Write-/Partial-Erkennung aus F11/F13/F16 haelt jetzt auch gegen
  adversariale Pfade (Cap-Exit mit Summary, Provider-Fehler nach
  Teil-Write) — deterministische Verifikation der Write-Intention statt
  Vertrauen in Outcome-Labels.
- **F19 — Store ohne eingehende Links erzeugt Orphans (2026-08-01, bestaetigt;
  Daten repariert 2026-08-01; RESOLVED 2026-08-01, BACKLINK_VERIFICATION).** Der 0.15.0-Abschluss-Store legte
  `/athenaeum/deep-code-review-0.15.0-lessons` an — angeblich "back-linked
  from lessons-0.14.0 and phase3-lessons" — doch `library_status` meldete
  das Konzept und `/athenaeum/multi-worker-foundation` als Orphans.
  Log-Beweis (`log.md`, 2026-07-31): genau zwei **Creations**, null
  **Updates** — die behaupteten Backlinks wurden nie geschrieben, obwohl
  Prompt-Regel 2 ("BACK-LINK AT CREATION") sie im selben Write-Flow
  verlangt. Deckt sich mit F13/F16-Muster: die Store-Summary behauptet
  Enrichment, die nicht stattgefunden hat. Learning: nach jedem Store mit
  relates_to/backlink-Absicht `library_status` pruefen (Orphans-Sektion),
  bevor man die Verlinkung als erledigt annimmt. Nebenbefund: Harness-seitig
  verwirft die Kimi-Code-Session die `mcp__athenaeum__*`-Tools, wenn der
  Container weg ist (Rebuild) — erst Session-Neustart registriert sie neu;
  Ad-hoc-Tests laufen dann per rohem JSON-RPC (initialize → tools/call),
  Skript: `scripts/mcp_live_test.py`.
  Resolution (2026-08-01, BACKLINK_VERIFICATION): `store_knowledge` und
  `update_knowledge` liefern jetzt ein deterministisches `links_after`-Feld
  (Post-Run-Link-Graph-Scan der geschriebenen Konzepte: `checked`,
  `unbacklinked`, `orphans`, `healthy`), und die Summary endet mit einem
  "Post-run check"-Urteil — behauptete Backlinks, die nie geschrieben
  wurden, fallen sofort im Tool-Ergebnis auf.
- **F20 — Bare-Path-"Related concepts"-Zeilen sind fuer den Graphen unsichtbar
  (2026-08-01, RESOLVED 2026-08-01, BACKLINK_VERIFICATION).** Bei der F19-Reparatur: `library_maintain` schrieb
  die fehlenden Backlinks korrekt in die Dateien — als nackte Pfade in
  "Related concepts"-Listen — und `library_status` meldete beide Konzepte
  **weiterhin** als Orphans. Ursache: `LINK_RE` in `library/links.py`
  extrahiert nur `[text](target)`-Markdown-Links; der Orphan-Check
  (`validate.py`: kein inbound UND kein outbound) sieht Bare-Pfade weder
  als eingehend noch als ausgehend. Der Librarian schreibt "Related
  concepts" aber habituell als `/pfad.md`-Liste, und weder die
  Write-Disziplin-Regel 2 noch das `MAINTAIN_TASK_TEMPLATE` pinnen die
  Link-Syntax — Maintain-"Reparaturen" sind damit strukturell unsichtbar
  fuer den Health-Check (F18/F19-Muster eine Ebene tiefer: Maintain-Summary
  behauptet Reparatur, Status zeigt sie nicht). Reparatur gelang erst per
  `update_knowledge` mit expliziter `[Titel](/abs/pfad.md)`-Anweisung
  (danach: 0 Orphans, healthy). Fix-Kandidaten: (a) Link-Syntax in Prompts
  pinnen + Vertrags-Test in `test_prompts.py`; (b) deterministische
  Backlink-Verifikation nach Store/Maintain (Post-Run-Orphan-Rescan der
  betroffenen Konzepte, analog Write-Intention-Pruefung/`health_after`).
  Resolution (2026-08-01, BACKLINK_VERIFICATION): beide Fix-Kandidaten
  umgesetzt — (a) Write-Disziplin-Regel 2 ("BACK-LINK AT CREATION") und das
  `MAINTAIN_TASK_TEMPLATE` pinnen jetzt die Markdown-Link-Syntax
  `[text](/absolute/path.md)` (Bare-Pfade sind fuer den Link-Graphen
  unsichtbar), abgesichert durch Vertrags-Tests in `test_prompts.py`; (b)
  `links_after` meldet geschriebene Konzepte ohne eingehende Links
  deterministisch im Store/Update-Ergebnis.
- **F21 — request_knowledge-Antwort enthielt Roh-Tool-Traces + falsche
  Coverage-Gaps (2026-08-04, Beobachtung, REMOTE-Instanz 0.20.0, UNRESOLVED).**
  Session-Start-Query (OKF/Curator/Librarian/Provenance) lieferte eine
  Antwort, die rohe Zwischen-Tool-Calls als JSON-Blobs einbettete
  (`{"path": "/athenaeum/versions/v0.4.2.md"}` -> "File not found",
  "Path not a directory" fuer `/athenaeum/versions`, "search_semantic
  requires a query string" trotz Query-aehnlichem Payload) und mit
  Coverage-Gaps endete — obwohl der Seed-Tree genau diese
  Versions-Dokumente listet und die Themen in der Library existieren.
  Zwei Teilbefunde: (a) F10-Mechanik (Prozess-Fragmente im `content`)
  in neuer Form — statt Synthese wird der Arbeits-Trace ausgegeben;
  (b) `read_document` scheitert an Seed-gelisteten Pfaden — Verdacht auf
  Pfadform-Mismatch (fuehrender Slash / `.md`-Suffix) oder Seed/Read-
  Inkonsistenz. Fix-Kandidat: Read-Pfad-Normalisierung pruefen und der
  Retrieval-Disziplin eine Regel "Seed-gelistete Pfade sind existent —
  bei File-not-found Pfadform variieren, nicht Coverage-Gap melden"
  pinnen.
- **F22 — `links_after` meldet unbacklinked trotz behaupteter Verlinkung
  (2026-08-04, Beobachtung, REMOTE-Instanz 0.20.0, UNRESOLVED).** Der
  v0.21.0-Abschluss-Store (`/athenaeum/versions/v0.21.0`) behauptete in
  der Summary "linked it from existing version v0.20.0 and the
  store-extensions lessons" — das deterministische `links_after` zeigte
  `unbacklinked: [/athenaeum/versions/v0.21.0]`, `healthy: false`.
  F19/F20-Muster auf 0.20.0 weiterhin reproduzierbar: die DETEKTION
  (links_after) funktioniert, aber der Librarian schreibt weiterhin
  behauptete Backlinks, die der Link-Graph nicht sieht.
- **F21 erneut bestaetigt (2026-08-04, REMOTE):** Session-Start-Query
  (Git-Versionierung/Time-Machine/Isolation) lieferte wieder ein
  Prozess-Fragment im `content` ("We have not read
  /ha-agenthub/ha-agenthub-architecture-lessons.md. Let's read it.") plus
  rohes Tool-Call-JSON — F10/F21-Mechanik weiterhin offen. Die gemeldeten
  Coverage-Gaps waren diesmal plausibel (Themen waren echtes Neuland).
- **F22 erneut bestaetigt (2026-08-07, REMOTE):** Der
  v0.23.0-Abschluss-Store behauptete in der Summary "Back-links added:
  Linked from existing version concepts v0.21.0 and v0.20.0" — das
  deterministische `links_after` zeigte `unbacklinked:
  [/athenaeum/versions/v0.23.0]`, `healthy: false`; kein einziger Backlink
  wurde geschrieben. Reparatur per `update_knowledge` mit expliziter
  `[text](/abs/pfad.md)`-Anweisung (F20-Pattern) funktionierte sofort;
  `library_status` danach healthy, 0 Orphans. Fazit unveraendert: nach
  jedem Store mit relates_to/backlink-Absicht `links_after`/`library_status`
  pruefen, bevor die Verlinkung als erledigt gilt.
- **F22 dritte Bestaetigung (2026-08-07, Deep-Review-0.23.1-Abschluss-Store):** Der
  Store der neuen `/athenaeum/lessons/deep-code-review-0.23.1-lessons` behauptete in
  der Summary "It is linked from the related concepts" (0.15.0-lessons, v0.23.0,
  search-performance-rework) — `links_after` zeigte erneut `unbacklinked`,
  `healthy: false`. Reparatur per `update_knowledge` mit expliziter
  `[text](/abs/pfad.md)`-Anweisung (F20-Pattern) funktionierte sofort
  (`healthy: true`). Befund unveraendert: Detektion (`links_after`) greift, die
  behaupteten Backlinks werden weiterhin nicht geschrieben — kein Fix seit 0.20.0
  erfolgt. Fix-Kandidat fuer die naechste Iteration: `store_knowledge` serverseitig
  nach dem Run die relates_to-Konzepte deterministisch nachverlinken (nicht dem
  Modell ueberlassen), oder die Store-Summary an `links_after` spiegeln.
- **F22 vierte Bestaetigung (2026-08-08, e5-base-Lessons-Store):** Der
  Abschluss-Store (`/athenaeum/lessons/embedding-model-intfloat-multilingual-e5-base-lessons`)
  behauptete "Linked from /athenaeum/versions/v0.12.0.md and
  /ha-agenthub/embedding-model-switch-e5-base-lessons.md" — `links_after`
  zeigte erneut `unbacklinked`, `healthy: false`. Reparatur per
  `update_knowledge` mit expliziter `[text](/abs/pfad.md)`-Anweisung
  funktionierte sofort (`healthy: true`). Befund unveraendert.
- **F22 sechste Bestaetigung (2026-08-17, Strip-Link-Targets-0.25.2-Lessons-Store):** Store
  von `/athenaeum/lessons/strip-link-targets-0.25.2-lessons` mit relates_to
  hybrid-search-0.19.0-lessons — Summary behauptete den Backlink, `links_after`
  zeigte `unbacklinked`, `healthy: false`. Reparatur per `update_knowledge` mit
  expliziter `[text](/abs/pfad.md)`-Anweisung sofort `healthy: true`.
- **F22 fuenfte Bestaetigung (2026-08-17, LOOP_GUARDS-Lessons-Store):** Store
  von `/athenaeum/lessons/agent-loop-guards-2026-08-17-lessons` mit relates_to
  v0.23.0 — Summary behauptete den Backlink, `links_after` zeigte
  `unbacklinked`, `healthy: false`. Reparatur per `update_knowledge` mit
  expliziter `[text](/abs/pfad.md)`-Anweisung sofort `healthy: true`.
  Zusaetzlich: der erste Store-Versuch (langer, mehrteiliger Content) endete
  im F13-Muster (No-Write → ToolError); die gekuerzte Version ging sofort
  durch — lange Stores weiterhin in kleinen Happen fahren.
- **F22 RESOLVED (2026-08-19, Fix nach sechs Bestaetigungen):**\r
  relates_to-Backlinking ist jetzt deterministisch serverseitig:\r
  `LibraryBackend.add_backlink` (Dedupe gegen modellgeschriebene Links,\r
  Normalisierung loser IDs wie `v0.23.0`, Self-Link-Guard, fehlende/\r
  mehrdeutige Ziele werden geloggt und uebersprungen — nie ein\r
  Run-Fehler) plus `Librarian._ensure_relates_to_backlinks` in\r
  `handle_store` UND `handle_update` (`update_knowledge` hat jetzt einen\r
  optionalen relates_to-Parameter). Die Backlink-Pflicht wandert vom\r
  Modell zum Server: Write-Disziplin-Regel 2 und der STORE-TASK-Text\r
  fordern keine relates_to-Backlinks mehr vom Modell (MUST NOT claim);\r
  das Ergebnis traegt ein additives `backlinked: [{id, title}]`-Feld\r
  (nur wenn nicht-leer), die Edits erscheinen als `updated`-Eintraege in\r
  `stored` (Embedding-Sync inklusive), und `links_after` verifiziert den\r
  Post-Backlink-Zustand. Die F19/F20-Detektion bleibt als Beweis\r
  bestehen; die Claim-vs-Reality-Luecke ist geschlossen.\r
- **F21 RESOLVED (2026-08-19, Fix im lokalen Tree):** Drei deterministische
  Massnahmen schliessen die F21-Luecken. (a) Answer-Hygiene-Guard in
  `BaseAgent._run` (`_dirty_answer_signals`: ganze Zeile = Tool-Call-JSON
  oder Argument-Blob, Prozess-Narration-Phrasen EN+DE, fenced Codeblocks
  ausgenommen) mit genau EINEM no-tools Re-ask ausserhalb des
  Iterations-Budgets, nur auf dem request_knowledge-Pfad (`answer_guard=True`
  in `handle_request`; Write-Task-Summaries duerfen JSON zitieren und
  bleiben unbewacht). Bleibt auch die zweite Antwort dirty, wird sie mit
  deterministischem Post-Run-Note-Marker zurueckgegeben, nie verworfen.
  (b) `read_document` probiert bei suffix-losem Pfad einmal `<pfad>.md` und
  meldet Verzeichnisse als "is a directory, use list_dir" statt
  "no such document" (Wrong-Tool statt Coverage-Gap). (c) Die finale
  Antwort landet (auf 2 KB gekuerzt) als Top-Level-`answer` im Trace —
  F21(a) ist damit nachtraeglich beweisbar und die Trefferquote des Guards
  per `.traces/*.json` messbar. Keine Prompt-Aenderung.
- **Deployment-Kontext (2026-08-04):** Die Kimi-MCP-Config
  (`~/.kimi-code/mcp.json`) zeigt auf `https://athenaeum.mzrsvr.net/mcp`
  (REMOTE, lief 0.20.0) — die Dogfooding-Library lebt dort, nicht im
  lokalen Dev-Container (localhost:8000, eigene app.db, Token-Label
  `vscode_kimi`). Session-Start-Queries und Stores gehen an REMOTE;
  lokale Rebuilds aendern daran nichts. Bei widerspruechlichem
  MCP-Verhalten immer zuerst pruefen, WELCHE Instanz gemeint ist.
  Nebenbefund: Auth-Fehler kommen als HTTP 200 mit JSON-RPC-Error im
  SSE-Stream ("Invalid or revoked bearer token") — Clients, die nur
  `result` auswerten, sehen faelschlich "leere" Antworten.
- **F23 — Suche konstant ~3,5 s durch Default-on-CPU-Rerank (2026-08-05,
  RESOLVED in 0.23.0).** User-Report: jede Suche ~3,5 s, auch
  aufeinanderfolgende (kein Cold-Start-Muster). Live-Messung im 2-CPU-
  Container mit echten ONNX-Modellen: der Cross-Encoder-Rerank
  (`Xenova/ms-marco-MiniLM-L-6-v2`) frass 3,5–5,4 s pro Suche — Kosten
  skalieren linear mit Kandidaten × Textlaenge (10×2000 chars ≈ 4,0 s;
  5×500 ≈ 0,3 s). Cold Start (Modelle memoized), FTS5 (~1,5 ms) und
  Vektor-Scan (<1000 Konzepte: ms) waren unschuldig. Aufloesung (0.23.0):
  `hybrid_rerank` Default auf OFF (DB/Agent/Backend; Bestandszeilen
  unveraendert), Rerank-Kandidaten 30→10 + Texte auf 2000 chars gekappt,
  In-Memory-Vektor-Cache (Scan 77 ms kalt → 0,3 ms warm), `top_k`/
  Hydration via `asyncio.to_thread`, NumPy-Cosine mit Stdlib-Fallback,
  per-Stage-DEBUG-Timings. Ergebnis: 0,30–0,40 s warm (rerank off).
  Learnings: (1) der Reranker ist die einzige Suchkomponente, die
  Dokumente komplett liest — der Librarian liest Top-Treffer ohnehin per
  LLM, daher ist Rerank auf CPU kein sinnvoller Default; (2) Stage-Timings
  VOR der Fix-Auswahl einbauen — die erste Fix-Runde (30→10, 2000 chars)
  half kaum, weil der Preis pro Token das eigentliche Problem war;
  (3) Skript fuer kuenftige Messungen: `scripts/search_perf_bench.py`.
- **F25 — Librarian schreibt literale `\uXXXX`-Escape-Sequenzen in Konzeptdateien
  (2026-08-06, Beobachtung, lokale Dev-Library, RESOLVED (2026-08-06,
  Write-Path-Guard: Auto-Decode in create_concept/edit_concept
  + Curator-Hygiene-Sweep repariert Bestand deterministisch (handle_curate))).** User-Report
  "komische Zeichen" in der neuen serverseitigen Markdown-Ansicht:
  `ha-agenthub-architecture-lessons.md` zeigte `DP\u20111` statt `DP‑1`.
  Byte-Check: die `.md`-Dateien enthalten die Escape-Sequenzen **literal als
  ASCII-Text** (Rendering war korrekt, Daten sind dirty). Betroffen: 4
  Dateien, ~120 Vorkommen (`\u2011` 84x, `\u2014` 28x, `\u202f` 18x, `\u2013`
  1x) — ausschliesslich Lessons-/Versions-Dokumente, also
  Librarian-Schreibartefakte (Modell emittierte `\\u2011` im Tool-Call-JSON
  statt des echten Zeichens). War nie aufgefallen, weil die alte Client-seitige
  Ansicht denselben Literaltext zeigte — kein Rendering-Regression. Entscheidung:
  Auto-Decode-Guard im Write-Path (`library/escape_guard.py`, aufgerufen in
  `create_concept`/`edit_concept`) — literale `\uXXXX` ausserhalb von
  Code-Spans/Fences werden deterministisch dekodiert und als `warnings`-Eintrag
  im Tool-Result gemeldet; ein Edit ohne `new_body` rescannt den Bestand nicht.
  Einmal-Reparatur der 4 dirty Dateien vom User abgelehnt.
  Nachschau (2026-08-06): die Code-Span-Semantik ist von stiller Ausnahme zu
  LLM-Judgment geworden — Escapes in Code-Spans/Fences bleiben byte-identisch,
  erzeugen aber warn-always einen `warnings`-Eintrag mit Zeile/Snippet
  (unterdrueckbar per `allow_literal_escapes: true` auf write_concept/
  edit_concept), und der Bestand wird ueber den neuen Curate-Finding-Key
  `code_span_escape_candidates` dem Curator-LLM zur Beurteilung gemeldet
  (struktureller Zustand, re-report bis gefixt, L14).
- **F24 — "Scheduler laeuft nicht" war nicht reproduzierbar; Observability-
  Luecke statt Defekt (2026-08-05, RESOLVED in 0.23.0).** User-Report: der
  automatische Curator laufe nicht, keine Traces/Activity sichtbar, Schedule
  "aktiv". Live-Test im lokalen Container (0.22.0, Schedule auf +3 min
  gesetzt): feuerte exakt zur Minute, maintain+curate liefen, zwei
  Activity-Zeilen mit `token_label="scheduler"`, `curate_last_run_at`
  re-baselined. Remote (athenaeum.mzrsvr.net) laeuft ebenfalls 0.22.0 —
  kein Versions-Bug. Die eigentliche Luecke: der Schedule-Zustand
  (aktiv/inaktiv, letzter Lauf) war in der WebUI unsichtbar; ohne
  DB-/Log-Zugriff ist "laeuft er?" nicht beantwortbar. Zusaetzliche
  UX-Falle: der Curator-Tab hat zwei getrennte Forms mit eigenem
  Save-Button — die Schedule-Checkbox ohne Klick auf den Schedule-Save
  wird nie persistiert. Aufloesung (0.23.0): Statuszeile im Curator-Tab
  (aktiv/inaktiv, UTC-Zeit, `curate_last_run_at`); `health_after` im
  Curate-Result meldet jetzt auch `broken_links`. Diagnose-Tricks:
  `/openapi.json` verraet die Version einer Instanz unauthentifiziert;
  gleiche oeffentliche IP von Dev-Maschine und Server (NAT) tauscht
  "lokal == remote" vor — immer anhand der Activity-DB pruefen, WELCHE
  Instanz die eigenen MCP-Calls journaliert.
- **F26 — Embedding-Shortlist bot fastembed-unladbare Modelle; stille
  Degradation der Semantiksuche (2026-08-17, REMOTE, RESOLVED in 0.25.3).**
  Live-DB hielt `intfloat/multilingual-e5-base`, das fastembed 0.8.0 gar nicht
  laden kann (nur `multilingual-e5-large` wird unterstuetzt) — Index-State
  "failed" auf der Statuskarte, aber `search_semantic` degradierte still zur
  Metadaten-Fallback und lieferte bei leerem Fallback `[]` (ununterscheidbar
  von "keine Treffer"); der Trace notierte dazu `fallback: false`,
  byte-identisch zu einem gesunden Leerergebnis. Aufloesung: Shortlist korrigiert
  (e5-large 1024-dim als Default, e5-base/e5-small entfernt);
  `LocalFastembedProvider._model` lehnt Off-Shortlist-Modelle mit
  WebUI-Rekonfigurations-Hinweis ab; leerer Metadaten-Fallback wirft jetzt
  einen RuntimeError mit gechainter Ursache statt `[]` zurueckzuliefern.
  Learning: kuratierte Modell-Listen gegen die tatsaechlich installierte
  Runtime-Registry pruefen, nicht gegen Doku-Annahmen.
- **F27 — LINK_RE zaehlt Beispiel-Links in Inline-Code/Fences als broken
  (2026-08-25, Beobachtung, REMOTE-Instanz, RESOLVED 2026-08-25,
  Code-Span-/Fence-Guard im Link-Pfad).** `library_status`
  meldete einen broken link `strip-link-targets-0.25.2-lessons.md` → `url`.
  Byte-Check ueber den Librarian: die Zeile ist „Inline markdown links
  `[text](url)` are transformed to `text` before …“ — Beispiel-Markup in
  Inline-Code-Backticks, in genau dem Lessons-Doc ueber das Link-Stripping
  (0.25.2). Ursache: `LINK_RE` in `library/links.py` (Zeile 24) matcht per
  `finditer` roh ueber den gesamten Body, ohne Inline-Code-Spans oder
  Fences auszulassen; die Fence/Span-Awareness existiert bereits in
  `library/escape_guard.py` (`_split_fence_segments`, `_iter_code_spans`),
  wird aber im Link-Pfad nicht genutzt. Folge: ein einziger False Positive
  haelt die Library permanent `healthy: false`, und da `handle_maintain`
  (L16) bei unhealthy immer einen vollen LLM-Run faehrt, zahlt jeder
  Scheduled-Maintain-Lauf LLM-Kosten fuer ein nicht reparierbares Finding;
  der Curator kann das Beispiel-Markup nicht "reparieren", ohne das
  Dokument zu beschaedigen. Fix-Kandidat: Body-Link-Extraktion in
  `links.py` um die escape_guard-Segmentierung ergaenzen (Links in
  Code-Spans/Fences nicht als Konzept-Links werten) + Vertrags-Test in
  `test_links.py`. Bis dahin: solche Findings als False Positive
  klassifizieren statt Maintain dagegen laufen zu lassen.
  Aufloesung (2026-08-25): Segmentierungs-Helper in neues Shared-Modul
  `library/md_spans.py` extrahiert (inkl. neuem `iter_code_segments`;
  `escape_guard.py` re-exportiert die privaten Namen, embeddings.py laeuft
  unveraendert) — bricht den Zirkularimport strukturell. `extract_body_links`
  UND der `move_concept`-Rewrite-Pfad (`_rewrite_body`) werten Links in
  Inline-Code-Spans und Fences jetzt symmetrisch nicht mehr als
  Konzept-Links (Extraction-Rewrite-Symmetrie, Muster: `strip_link_targets`).
  Vertragstests: 6 in `test_links.py`, 3 in `test_validate.py` (inkl.
  code-span-only-inbound → weiterhin orphan), 1 F22-Dedupe-Pin in
  `test_backend_writes.py`. Suite: 1018 passed, ruff clean. Folge-Effekt:
  der Strip-Link-Targets-False-Positive verschwand mit dem Deploy auf
  0.26.2 (2026-08-27/28, live verifiziert: Maintain-No-op ohne LLM-Run).
  Follow-up (2026-08-28 erledigt, 0.27.1): `webui/graph.py` hatte einen
  eigenen Display-only-Regex mit derselben False-Positive-Klasse — die
  Graph-Edges nutzen jetzt denselben `iter_code_segments`-Guard
  (`_body_link_targets`), Vertragstest in `test_graph.py`.
- **F28 — `library_status` reduziert Nicht-Graph-Warnings auf einen nackten
  Zaehler (2026-08-25, Beobachtung, REMOTE-Instanz 0.26.1, RESOLVED
  2026-08-25 (0.27.0)).**
  Nach dem F27-Link-Fix zeigte `library_status` `warnings: 10` — aber
  `backend.status()` (backend.py:404) listet nur `orphans` und
  `broken_links` einzeln auf; alle uebrigen Warnings
  (`missing-recommended`, `malformed-date`, `index-drift`,
  `footnote-mismatch`, ...) kollabieren zu `"warnings": len(warnings)`.
  Ueber die MCP-Oberflaeche ist damit nicht diagnostizierbar, WAS die
  Warnings sind — nur ueber WebUI (`/library/validate`) oder DB-Zugriff.
  F24-Muster (Observability-Luecke statt Defekt): "gesund minus bekannte
  False Positives?" ist ohne internen Zugriff nicht beantwortbar, und der
  Agent kann Warnings weder priorisieren noch dem Curator zielgerichtet
  melden. Fix-Kandidat: `warnings_by_code: {code: count}` plus eine
  itemisierte, geboundete Liste (analog orphans/broken_links, z. B.
  first-N mit Pfad + Code) ins Status-Result; MCP-Vertrag additiv
  erweitern, keine Prompt-Aenderung noetig.
  Resolution: `status()` liefert jetzt `health.warnings_by_code`
  (vollstaendiges {code: count}) und `health.warning_items` (erste 50
  Validate-Eintraege verbatim, `MAX_STATUS_WARNINGS`); Zaehler bleiben
  ungekuerzt, MCP-Vertrag additiv erweitert.
