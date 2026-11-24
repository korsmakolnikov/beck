---
title: "beck: un orchestratore deterministico per sviluppare con gli agenti"
author: "Federico Caprari (github.com/korsmakolnikov)"
date: "24 novembre 2026"
lang: it-IT
---

# Il problema

## Perché beck

- minime: un workflow architecture-first fatto solo di agenti
- Costa troppi token: unclebob 70,8 %, joshua 10,7 %, kent 6,4 %
- È lento, e chiede l'utente a ogni componente
- Un orchestratore LLM rilegge tutta la sessione a ogni passo
- beck sostituisce l'orchestratore con codice deterministico

::: notes
Su hello world il kent di minime ha letto 9,3 M token dalla cache: tiene
l'intera sessione in un solo contesto, e ogni ritorno di un sub-agente in
background è un nuovo turno che la rilegge. joshua (l'audit di ogni turno) da
solo è il 24 % dei token di quel run. beck non dipende da nulla di minime:
ne riscrive le idee buone nei suoi termini (docs/minime-agents.md).
:::

## L'obiettivo

- Una macchina che programma da sola, con l'umano come controllore
- L'umano è l'architetto: dominio, architettura, metodo
- beck è l'ingegnere: scrive test-first, verifica ogni passo, si corregge
- L'utente approva all'inizio e fa la review alla fine
- Può girare di notte: ciò che non risolve lo parcheggia e va avanti

::: notes
Ogni regola si pesa contro questo obiettivo: una regola che ferma la macchina
per un dettaglio che un controllore non guarderebbe (una riga che nessun test
esegue) è una regola sbagliata. Un finding che il controllore deve vedere va
nella review, senza fermare il lavoro. L'obiettivo di lungo periodo: beck
riscrive beck, come un compilatore che fa il bootstrap di se stesso.
:::

# Come funziona

## I principi

- Prima il deterministico, l'agente solo come fallback
- Input e output degli agenti in JSON, ciascuno con il suo schema
- Gli eventi sono l'unica fonte di verità (CQRS ed event sourcing)
- Gli agenti non committano: committa beck, dopo aver verificato
- Il grafo è il punto di sincronizzazione: ogni agente riceve solo la sua fetta

::: notes
Chain of responsibility: un handler deterministico risponde quando può,
altrimenti l'evento passa a un agente. Un output fuori schema è
SignalRejected e il turno si ripete. Ogni agente scrive solo nel suo scope
(src/beck/agents/scopes.py): un turno che tocca un file fuori scope viene
annullato e rifiutato. Le parole dell'utente diventano azioni attraverso kent,
scelte da un menu chiuso (RouteDecision).
:::

## Chi fa cosa

:::: {.columns}
::: {.column width="50%"}
**beck, deterministico**

- State machine, policy, scheduler
- Test, check, commit, squash, atterraggi
:::
::: {.column width="50%"}
**Gli agenti**

- kent: lo scope, la shopping list, le decisioni
- hughes e ford: il dominio, l'architettura e le interfacce
- unclebob (TDD) e martin (refactor e review)
:::
::::

::: notes
Ogni comando è un turno nuovo con un contesto piccolo: unclebob ha una
sessione per ciclo (una sessione per unità era arrivata a 832k token per
turno). hughes e ford, fusi in kent il 13 novembre, sono tornati il 23
novembre: l'architettura ad agenti di minime era giusta. Tolti: joshua
(sostituito dai controlli deterministici), simon, marvin. argyris, l'agente
che migliora gli agenti, per ora è un log (.beck/argyris.log).
:::

## Le DSL e il grafo

- Una spec YAML per dominio: entità, morfismi, invarianti, proprietà
- L'architettura in YAML: componenti, interfacce, asserzioni, dipendenze
- Un indice SQLite: spec id → componente → simboli → test
- Le fitness function generate dalle asserzioni
- Ogni agente riceve solo il nodo su cui lavora

::: notes
ADL abbandonato per una DSL YAML nello stile della spec, con un id su ogni
elemento. Nessuna components map: chi implementa cosa sta nell'architettura.
I simboli vengono da tree-sitter (Python, Elixir, C). Il codice cita i
requisiti con i marker `spec: <id>` nelle docstring. `covers` lega ogni voce
della shopping list ("Delivers", "Done when") ai requisiti che la provano:
una voce scoperta è un errore di validazione.
:::

## Le sei fasi

- Inquadramento: intervista, shopping list, branch
- Survey e modello: toolchain, dominio, architettura, interfacce
- Al giorno zero: gli acceptance test, rossi sugli unimplemented
- Implementazione e refactor: componenti in parallelo, ciascuno nel suo worktree
- Integrazione e chiusura: suite completa, check, review dell'utente

::: notes
Le fasi vengono da un event storming; ogni ramo è una transizione di una
state machine. L'utente approva tre documenti (shopping list, dominio,
architettura) e fa la review del branch. Gli acceptance test li scrive
unclebob, uno per turno e uno per invariante, da "Done when"; beck li accetta
solo rossi per un unimplemented e li committa prima di ogni componente. Dopo
ogni ciclo girano nel worktree del componente: un TypeError o un
AttributeError è un'interfaccia tradita (AcceptanceMisaligned).
:::

## Un ciclo TDD verificato

- unclebob scrive il test rosso e lo lancia con `beck send test`
- beck verifica il rosso su un `TestsRun` fresco e committa
- unclebob scrive il verde; beck verifica che nessun test sia cambiato
- martin rifà i test a codice fermo, poi il codice a test fermi
- Un commit per componente: i cicli si squashano, il resto è un fixup

::: notes
L'evidenza è il report, mai la parola dell'agente: beck accetta una fase solo
su un TestsRun il cui tree git è quello del working tree al momento della
verifica, con i test dichiarati nell'esito atteso. Un errore di sintassi o di
collection non è un rosso. L'agente non legge mai la console: riceve ALL_OK
oppure una riga per test fallito. Un test debole trovato da martin torna a
unclebob come ciclo riaperto al rosso. L'utente vede un commit per
componente; le sue correzioni sono fixup, visibili fino alla chiusura.
:::

## Event sourcing

- Un event store SQLite per iterazione; ogni payload ha uno schema
- causation_id e correlation_id: `beck trace` segue ogni catena
- Un crash annulla la transazione; `beck attach` riprende dopo il fix
- Replay di un'iterazione: agenti live, registrati o misti
- I consumi sono una proiezione: ogni numero è una somma di eventi

::: notes
I comandi non si memorizzano mai, solo gli eventi. Ogni input (l'output di un
agente, la risposta dell'utente) si elabora in una transazione: i fatti, poi
le policy di ogni nuovo evento. Un crash registra BeckCrashed; dopo il fix
l'input si rielabora senza richiamare l'agente (CrashRecovered). Un turno in
cui beck è morto riparte dal suo snapshot. `beck rewind` rifà un ciclo
dall'evento che l'ha aperto e tiene il primo run annullato, per confrontare
due modelli sullo stesso ciclo.
:::

## Quando si blocca

- Firma del fallimento: un blocco ripetuto 3 volte blocca il ciclo
- Tetto per componente: 150 turni o 5 M token pesati
- Stall detector: lo stesso turno due volte, o 10 turni senza progressi
- Componente parcheggiato: kent propone un restart, l'utente lo approva
- Nessun vicolo cieco: con niente da fare, `SessionIdle` chiede all'utente

::: notes
La firma è normalizzata: (test, tipo di fallimento, messaggio) senza numeri
di riga, indirizzi e tempi. Con p = 1 è lo stesso risultato tre volte, con
p ≥ 2 uno schema (A B A B A B). Lo stall detector nasce dal pilot hello
world: martin ha fatto 1.228 turni di refactor su un componente, senza
fallire nulla, con 306 M token letti dalla cache. Un restart è un diff reale
sul branch: l'utente lo rivede, beck lo committa come fixup dei commit che
tocca e riapre i cicli.
:::

# I numeri

## Come si misura

- Token pesati: input 1, scrittura in cache 1,25, lettura 0,1, output 5
- beck: gli eventi `AgentTurnEnded` dell'iterazione
- minime: i transcript di Claude Code, ogni messaggio contato una volta
- Le due fonti concordano: +0,7 % su paddles (i turni falliti)
- Pesi uguali per tutti i modelli: si confronta il lavoro, non il prezzo

::: notes
Tutto viene da `beck benchmark`: niente è stimato, niente viene da una
bolletta. I pesi sono quelli dell'analisi dei costi di minime. Cosa i numeri
non dicono: come si guida un workflow pesa (su paddles il kent di minime
faceva polling sui suoi agenti: 1.501 turni, 420 M token letti); i token di
chi guida il benchmark non sono in nessuno dei due conti.
:::

## beck contro minime

:::: {.columns}
::: {.column width="50%"}
**Primo giro (beck su sonnet)**

- hello 0,06 · pingpong 0,21
- paddles 0,06 · alien-attack 0,18
:::
::: {.column width="50%"}
**Rerun sui modelli di minime (opus)**

- hello 0,11 · pingpong 0,36
- paddles 0,14 · alien-attack 0,70
:::
::::

::: notes
Rapporto fra token pesati, beck diviso minime: 0,06 vuol dire che beck ha
speso il 6 %. Paddles senza il kent di minime: 0,25; con i tentativi
bloccati del rerun: 0,19. Hello world: 199.101 contro 3.530.104. Paddles: 3,5 M contro
57,1 M, gonfiati dal polling di kent. Alien-attack nel primo giro: 4,8 M
contro 26,8 M, ma il gioco di beck era ingiocabile (prossima slide). Nel
rerun alien-attack ha chiuso a 18,6 M; con il round fermato dai bug arriva a
29,6 M, cioè 1,10. La condizione di merge del branch: beck funziona e spende
meno di minime.
:::

## Cosa ha insegnato alien-attack

- 121 test verdi, un gioco che nessuno poteva giocare
- Ogni componente provava la sua metà; il launcher non chiamava lo schermo
- Una regola diceva cinque cose: un ciclo, un test, una cosa provata
- Un test lasciato acceso ha fatto crescere un gioco fino a 24 GB
- Le risposte: una regola un'affermazione, la piramide dei test, limiti di memoria

::: notes
Lo schermo era costruito e testato ma mai chiamato; il file dei punteggi era
Path("."); ogni alieno compariva in colonna 0. Gli acceptance test
controllavano solo l'exit status e la riga finale. Dopo: la regola "one rule,
one claim" (e uno score delle congiunzioni pesato da hughes), test_pyramid che
mappa le DSL ai test per tipo (solo gli acceptance test lanciano il
programma), il plugin pytest di beck che misura memoria e secondi di ogni
test e uccide i processi orfani. Nel rerun il gioco funzionava, con una
correzione in review (schermo alternativo, cursore).
:::

## Il giro r2: rieseguire finché è pulito

- La regola: un run con un errore si archivia, si corregge, si riparte
- hello world: pulito all'ottavo run, dopo 11 fix
- Costo del run pulito: 0,15 di minime
- I bug trovati: coverage, formatter, unimplemented nascosti, grafo vecchio
- Gli agenti ora chiedono le parole ambigue e i testi che l'utente legge

::: notes
Hello world r2: 519.416 token pesati contro 3.530.104. Con i sette tentativi
fermati arriva a 1,16, ma quei tentativi misurano i bug di beck, non il
workflow. Esempi di fix: coverage.py leggeva i path come nomi di moduli; i
file generati non passavano il formatter del progetto; un acceptance test che
confronta prima stdout nascondeva il NotImplementedError del programma
(ora un sitecustomize nel plugin lo annota); "touches no file" letto come
"non legge nessun file" produceva una regola impossibile. pingpong è in corso
con la stessa regola.
:::

# Il futuro

## Prossimi passi

- Mutation test dopo il refactor di ogni componente
- Il costo in tempo e memoria misurato su input crescenti
- Una scala di review fra il ciclo e il componente
- Modalità confronto: lo stesso ciclo su due modelli, sceglie l'utente
- Un agente front-end: si parla con beck, l'orchestratore diventa minime

::: notes
Mutation test: un mutante che sopravvive è un test debole, va a martin; un
budget di round per componente evita loop infiniti. Il costo: non decidibile
in generale (Rice), ma misurabile contando allocazioni e passi su input di
taglia 10, 100, 1000 generati da un seed, e con lo steady state per i loop.
Altro: il replay da un evento qualsiasi di un pilot, l'osservabilità per
girare in CI/CD, lo split deterministico delle regole sopra soglia. La prima
release: minime viene deprecato, questo progetto prende il nome minime e
l'agente front-end prende il nome beck.
:::
