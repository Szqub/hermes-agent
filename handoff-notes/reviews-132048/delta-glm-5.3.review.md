Wszystkie weryfikacje wykonane. Raport poniżej.

VERDICT: **APPROVE**

Env: repo na HEAD cd19fea3, drzewo czyste przed i po (sha256 agent/relay_llm.py = 286ff35f0ec9a0c74f907cef061f597be537e77a2f79ddfad1562633d3d11b42 przed i po teście parent-swap; `git status --short` pusty, `git diff HEAD --stat` pusty).

---------------------------------------------------------------------
1. Czy claim jest szczelny — analiza czterech ryzyk
---------------------------------------------------------------------

(a) close podczas gdy worker jest wewnątrz next() — NIEMOŻLIWE.
Jedynym wejściem do `next(raw_iterator)` jest `_read_next_chunk`
(relay_llm.py:401); `raw_iterator` powstaje w :428 (`iter()` nie
wykonuje generatora), `_next_provider_chunk` wołany tylko z :401
(sprawdzone grepem — brak innych call-sites). Worker ustawia
`reading=True` pod `close_guard` (:396-399) ATOMICZNIE z checkiem
`closed`. Zamykający w `_claim_close` bierze TEN SAM lock (:372):
albo widzi `reading=True` → odkłada (`close_requested=True`,
:375-377, zwraca False — `close()` się nie wykonuje), albo ustawia
`closed=True` pod lockiem, ZANIM wywoła `close()` poza lockiem
(:378, :385-387). W oknie między claimem a faktycznym `close()`
worker nie może wejść do iteratora, bo jego entry-check czyta
`closed` pod tym samym lockiem i zwraca `(None, True)` bez
dotykania iteratora (:397-398). To jest dokładnie domknięcie luki
z e5dbd9f7 (tam decision był pod lockiem, ale act — już nie).

(b) podwójny close — NIEMOŻLIWE. `closed` przechodzi False→True
wyłącznie pod `close_guard` (:378) i nigdy nie wraca do False.
`run_callback(close)` wykonuje się tylko gdy `_claim_close()`
zwróciło True, co może się zdarzyć dokładnie raz na inwokację
`_provider_stream`. Ścieżka odroczona (:411) woła tego samego
`_close_when_safe`, więc korutyna-finally i deferred-worker
konkurują o jeden mutex-claim — dokładnie jeden wygrywa.

(c) nigdy nie zamknięty — jedyny reachable przypadek to worker
zablokowany w `next()` na zawsze (provider nie wraca); wtedy
`_close_when_safe` zwraca bez zamykania, zgodnie z projektem i
dokumentacją ("bounded by the provider", doc :79-82). Wszystkie
inne przeploty zamykają: (i) closer claimuje, gdy worker jeszcze
w kolejce executora → worker early-returnuje; (ii) worker w
`next()` → closer odkłada → worker w finally czyści `reading` i
czyta `deferred` pod lockiem (:403-405); jeśli closer zdążył
najpierw ustawić `close_requested`, worker zamknął; jeśli worker
zdążył najpierw skasować `reading`, closer zamknął inline. Wątek
`closed` ustawiony na surowej ścieżce bezwarunkowo kończy
próby. Trywialny edge: raw_stream=None (factory rzuciło) →
`getattr(None,"close")` → no-op, nic do zamykania.

(d) deadlock/busy-wait — BRAK. `close_guard` jest trzymany
wyłącznie dla odczytu/zapisu flag (nigdy w poprzek `next()` ani
`close()`), `run_callback(close)` wykonuje się poza lockiem,
zamykający nigdy nie czeka na workera, worker nigdy nie czeka na
zamykającego. Deferr jest non-blocking. Brak pętli czekających.

Potwierdzenie empiryczne: napisałem harness replikujący dokładnie
closure z cd19fea3 (ten sam kod `_claim_close`/`_close_when_safe`/
`_read_next_chunk`) i młotkowałem 1 closerem + 4-5 readerami,
800 przebiegów: `OK: 800 interleaving runs, exactly-one-close=True,
no close-during-next, no hang, early (None,True) after close
verified`. Plik: /opt/data/cache/scratch/handshake_stress.py
(poza repo, scratch).

Dodatkowo sprawdzone: tylko jeden `_read_next_chunk` w locie na
inwokację (pętla `await asyncio.to_thread(...)` jest sekwencyjna,
:433); dwie inwokacje `_provider_stream` nie współdzielą
generatora ani locka (każda ma własny closure).

---------------------------------------------------------------------
2. Konsekwencja early `(None, True)` w _read_next_chunk
---------------------------------------------------------------------

NIESZKODLIWE — flaga w ogóle nie jest ustawiana na tej ścieżce.
Aby worker zobaczył `closed=True`, `_claim_close` musiało wcześniej
wygrać, a jego jedynym pierwszym callerem jest finally korutyny
(:447) — deferred path (:411) wymaga już wcześniejszego
`close_requested`, indukcyjnie też od finally. Czyli gdy worker
early-returnuje, blok try korutyny już się zakończył, await
w :433 się nie wznowi, a wynik trafia do przyszłości, której
nikt nie czyta. `self._provider_completed = True` z gałęzi
`exhausted` (:441) jest przez to nieosiągalne z early-return.

Hipotetycznie (edge asyncio: cancel dostarczony po done future)
nawet gdyby korutyna wznowiła z `(None, True)`, semantyka flagi
byłaby poprawna — provider po close JEST zakończony. Jedyny
konsument: `_recoverable_relay_failure` (relay_llm.py:513), jego
klienci to `_start_managed` (:485-487) i `__next__` (:567-569) →
`_preserve_pending_provider_chunks` (:589). Replay `_raw_chunks`
to udokumentowany fallback ("provider success, Relay post-processing
failed"), a retriggers podwójnej dostawy chroni `_delivered_unmatched`
(:514-520). Reachable harm: brak.

---------------------------------------------------------------------
3. Dokumentacja
---------------------------------------------------------------------

Dokładna wobec kodu, w tym sekcja "The handshake":
- `_claim_close`: backoff na `reading` (:375-377) i na `closed`
  (:373-374), claim przez `closed` atomically z checkiem — zgadza
  się z :372-379.
- `_read_next_chunk`: early exhausted bez dotykania iteratora,
  `reading` inaczej — zgadza się z :396-399.
- "the worker re-checks `closed` under the same lock before
  entering the iterator" — zgadza się z :396-398 (to jest
  właśnie fix luki e5dbd9f7).
- "logs rather than raises" dla deferred close — :412-417.
- "Consequences to preserve" — lease/loop zwalniane niezależnie od
  inline/deferred: zgodne z :631-645; brak wyjątku z deferred
  close: zgodne z :410-417.
- Sidebar: wpis dodany (website/sidebars.ts, diff HEAD potwierdza).

Dwa nit-y (nieblokujące, słowne):
- relay-managed-stream-ownership.md:73-74 — "the worker re-checks
  `closed`" brzmi jak drugi check; w kodzie to jedyny entry-check
  workera. Substancja OK.
- commit message: "Deferred closes are logged" — logowana jest tylko
  NIEPOWODZENIE deferred close (:412-417), nie samo odroczenie.
  Sugeruje log na každym defer.

---------------------------------------------------------------------
4. Test — wykazuje fix
---------------------------------------------------------------------

Na HEAD (4 uruchomienia):
  /opt/data/cache/scratch/ha-venv/bin/python -m pytest
    tests/agent/test_relay_blocked_generator_interrupt.py -q -p no:cacheprovider
  → "1 passed in 2.55s", powtórki: 2.51s / 2.54s / 2.49s (4/4 zielone)

Na parent bd0affe5 (swap read-only, potem restore):
  git show bd0affe5:agent/relay_llm.py > agent/relay_llm.py && pytest...
  → FAILED tests/agent/test_relay_blocked_generator_interrupt.py
    ::test_signal_during_blocked_provider
    AssertionError: ["ValueError('generator already executing')"]
    at tests/agent/test_relay_blocked_generator_interrupt.py:57
  (assert not close_errors — dokładnie oryginalny bug z issue)
  Exit=1. Po: `git checkout HEAD -- agent/relay_llm.py`, sha256
  identyczny z HEAD, `git status --short` pusty. Nic nie commitowane.

Test test_relay_llm.py na HEAD: "49 passed in 2.01s".

Znane 7 porażek środowiskowych potwierdzone:
  pytest test_relay_tools.py test_relay_runtime_plugins.py
  test_relay_atof_cwd.py → "7 failed, 35 passed in 6.48s"
  (nemo_relay initialize() additional_plugins_toml — wersja skew,
  reprodukuje się niezależnie od zmiany; ten sam błędny podpis
  widać w setup-logu udanego testu interrupt).

Test sam w sobie: POSIX marker zgodny z konwencją platforms()
(zgodnie z AGENTS.md — nie bare skipif), przywraca handler
SIGALRM w finally, joinuje workery z timeoutem, asseruje
zwolnienie `_loop` i `_runtime_lease`.

---------------------------------------------------------------------
5. Co musi być poprawione przed upstream
---------------------------------------------------------------------

Nic blokującego. Dwie opcjonalne uwagi (do uznania autora):
- Deferred close wykonuje `run_callback(close)` na wątku workera,
  a przed zmianą close zawsze biegł na threadie loop/konsumenta.
  Dla provider-ów bez thread-affinity w close() bez różnicy
  (run_callback i tak robi context.copy().run), ale warto mieć to
  na oku, gdyby jakiś SDK wymagał close z wątku tworzącego.
- Przeredagować zdanie "Deferred closes are logged" w commit
  message na "failed deferred closes are logged".

Wniosek: handshake jest szczelny względem wszystkich czterech
ryzyk, early-return nie dociera do żadnego szkodliwego konsumenta,
dokumentacja zgodna z kodem, test wykazuje fix w obie strony.
APPROVE.
