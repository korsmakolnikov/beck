---
title: "beck: un orchestratore deterministico per sviluppare con gli agenti"
author: "Federico Caprari (github.com/korsmakolnikov)"
date: "24 novembre 2026"
lang: it-IT
---

# minime

## L'architettura di minime

:::: {.columns}
::: {.column width="50%"}
**Il flusso**

- kent orchestra e inoltra le domande
- hughes scrive il dominio
- ford l'architettura e le interfacce
- unclebob i componenti, in TDD
- martin misura e rifattorizza
:::
::: {.column width="50%"}
**Intorno**

- joshua controlla ogni turno
- argyris corregge gli agenti
- simon documenta l'architettura
- marvin scrive AGENTS.md
- linus custodisce i commit
:::
::::

::: notes
minime è un workflow architecture-first fatto solo di agenti. kent è il thread
principale: inquadra il lavoro, scrive la shopping list, lancia gli altri e
riporta all'utente le loro domande, alla lettera. Prima il dominio (hughes),
poi l'architettura (ford), poi un componente alla volta, dall'interno verso
l'esterno (unclebob), e il refactor di ogni unità (martin); l'utente rivede il
commit di ogni unità. joshua e simon girano in background; argyris gira in una
sessione sua. I nomi: Kent Beck (TDD), John Hughes (QuickCheck), Neal Ford
(fitness function), Robert C. Martin (le tre leggi del TDD), Martin Fowler
(Refactoring), Chris Argyris (double loop learning), Simon Brown (C4), Marvin Minsky,
Linus Torvalds, e Joshua, il WOPR di WarGames.
:::

## Perché ha funzionato: i DSL

- hughes e ford, agnostici rispetto al codice, costruiscono i DSL
- Domain spec (hughes): entità, operazioni, invarianti, proprietà
- ADL (ford): sistema, domini, componenti, asserzioni
- Ogni regola della spec ha un id, e i test lo citano
- I DSL sono l'input strutturato degli altri agenti

::: notes
DSL: domain-specific language. Struttura: file di testo con una sintassi fissa.
La domain spec è YAML, validato da `spec-check`; l'ADL è lo pseudo-codice di
Richards e Ford.

Contenuto: la domain spec dice che cos'è il dominio e che cosa deve garantire,
senza nominare componenti; l'ADL dice com'è fatto il sistema e quali dipendenze
sono vietate. Una components map lega ogni id della spec al componente che lo
realizza.

hughes non legge codice: parte dalla shopping list e dalle risposte
dell'utente. ford si ferma alle interfacce e non scrive logica. Ragionano sul
problema, non sull'implementazione, e il loro contesto non si sporca con test,
log e tentativi falliti. Gli altri agenti non ricevono prosa: unclebob ricava
i test dalla spec, e l'ADL diventa fitness function.
:::

## Perché ha funzionato: il workflow TDD

- Le tre leggi di unclebob limitano la libertà dell'agente
- Ogni test lega l'output dell'agente all'intenzione
- L'intenzione è nei DSL: le invarianti, la struttura
- Passi precisi dove inserire controlli, in un contesto limitato
- Per esempio, la verifica della time complexity

::: notes
Le tre leggi: niente codice di produzione se non per far passare un test che
fallisce; niente test più grande di quanto basta a fallire; niente codice di
produzione oltre quello che fa passare il test. L'agente non può allargarsi:
validazioni, default e gestione degli errori che nessun test chiede sono
vietati.

Il test cita l'id della regola (spec: round-count-is-positive): dal codice si
risale all'intenzione, e una regola senza test si vede.

Ogni passo ha un inizio e una fine, quindi un punto dove controllare, e il
controllo guarda poche righe. In minime martin misura la complessità in tempo e
in spazio di ogni unità prima e dopo il refactor, e non la peggiora senza il
consenso dell'utente.
:::

## Perché ha funzionato: double loop learning

- Chris Argyris (1977): correggi la regola, non solo l'errore
- Il termostato: accendi, o ti chiedi se 20 gradi sono giusti?
- joshua controlla ogni turno e registra le violazioni
- argyris ne ricava le modifiche ai file degli agenti
- L'utente approva, e il prossimo run parte dalla regola nuova

::: notes
Nota storica. Chris Argyris (1923-2013), Harvard, studiava perché le
organizzazioni non imparano. Con Donald Schön distingue due cicli: nel single
loop learning correggi l'azione e lasci intatta la regola che l'ha prodotta;
nel double loop learning metti in discussione la regola. Il suo esempio è il
termostato: accendere il riscaldamento sotto i 20 gradi è single loop learning;
chiedersi se 20 gradi sia la temperatura giusta è double loop learning
(Double Loop Learning in Organizations, Harvard Business Review, 1977).

In minime: correggere il codice che un agente ha sbagliato è single loop
learning. joshua confronta ogni turno con il file dell'agente e registra le
violazioni senza fermare nessuno; argyris, in una sessione sua, propone per
ciascuna una modifica ai file degli agenti e delle skill e applica quelle
approvate. Cambia la regola: è double loop learning.
:::

## La domain spec di hughes: pingpong

```yaml
spec: 2
system: pingpong
entities:
  - name: RoundCount
    kind: value_object
    description: The number of rounds a game plays.
    fields: [{name: value, type: integer, required: true}]
    invariants:
      - id: round-count-is-positive
        rule: A round count is an integer of at least one.
operations:
  - id: play-pingpong
    description: A game runs ping and pong to the end.
    post: [The transcript holds exactly 2N lines.]
properties:
  - id: any-round-count-gives-the-alternating-transcript
    kind: oracle
    forall: [{name: rounds, generator: a count from 1 to 200}]
    holds: The transcript equals the list built from the count.
```

::: footer
Che cosa il dominio è e deve garantire: nessun componente, nessun codice
:::

::: notes
Pingpong: due attori, ping e pong, ciascuno con il suo thread e la sua mailbox,
si scambiano N round di messaggi. Un estratto, nel formato di minime: il
dominio è quello del pilot.

Le sezioni: entities (entità e value object, con le loro invarianti), events,
relations, constraints (regole che attraversano più entità), operations (pre,
post, errori, eventi emessi), properties (per i test property-based: invariant,
roundtrip, idempotence, metamorphic, model, oracle). hughes la scrive prima di
ogni architettura, e `spec-check` la valida. Ogni voce diventa almeno un test
rosso: qui la proprietà oracle diventa un test con Hypothesis su 1-200 round.
:::

## L'ADL di Ford e Richards: pingpong

```
ADL:PINGPONG SYSTEM

DESCRIPTION Two threaded actors exchange messages for N rounds
REQUIRES AdlFitness pytest helper
DEFINE SYSTEM Ping Pong AS pingpong
  DEFINE DOMAIN Actors AS actors
    DEFINE COMPONENT Mailbox AS mailbox
    DEFINE COMPONENT Actor AS actor
  DEFINE DOMAIN Ping Pong AS game
    DEFINE COMPONENT Ping AS ping
    DEFINE COMPONENT Pong AS pong
    DEFINE COMPONENT Game Runner AS runner
  DEFINE DOMAIN Command Line AS cli
    DEFINE COMPONENT Command AS command
ASSERT(CLASSES are only CONTAINED within COMPONENTS)
ASSERT(actors has NO DEPENDENCY on {game, cli})
ASSERT(ping has NO DEPENDENCY on pong)
ASSERT(pong has NO DEPENDENCY on ping)
```

::: footer
Ogni DEFINE diventa un namespace, ogni ASSERT almeno una fitness function
:::

::: notes
ADL, Architecture Definition Language, di Mark Richards e Neal Ford: uno
pseudo-codice che descrive e governa la struttura di un sistema, pensato perché
un LLM lo trasformi in codice e fitness function. Un estratto del pingpong, nel
formato di minime (nel pilot ci sono anche rounds e messages).

DEFINE dà a ogni elemento un nome leggibile e un nome fisico: il percorso dei
nomi fisici è il namespace, per esempio pingpong.game.ping. ASSERT vieta: ping
e pong si parlano solo attraverso le mailbox, quindi nessuno dei due può
dipendere dall'altro. In minime ogni ASSERT diventa un test pytest o ExUnit, e
una dipendenza vietata fa fallire la suite.
:::

## I limiti di minime

- I token finiscono prima di chiudere un solo componente
- È lento: ore per un singolo componente
- Copilot CLI: le policy di sicurezza fermano il flusso
- Comandi sempre diversi: pipeline bash spesso indecifrabili
- Ogni richiesta interrompe anche il lavoro parallelo dell'utente

::: notes
Sulla mia macchina i token finiscono prima che un componente sia chiuso.
Perché costa tanto: un orchestratore LLM rilegge tutta la sessione a ogni
passo. Su hello world il kent di minime ha letto 9,3 M token dalla cache:
tiene l'intera sessione in un solo contesto, e ogni ritorno di un sub-agente
in background è un nuovo turno che la rilegge. Come si dividono i token fra
gli agenti, nelle misure di minime: unclebob 70,8 %, joshua 10,7 %, kent 6,4 %.
Da sole queste percentuali dicono chi consuma, non se il costo è sostenibile.

Copilot CLI: gli agenti provano di continuo comandi nuovi, e ogni comando non
previsto dalle policy chiede un'approvazione. Il flusso si ferma, l'utente
viene distolto da quello che sta facendo, e per rispondere deve capire una
pipeline bash spesso illeggibile.
:::

# beck

## Perché beck

- La promessa: ridurre i costi e i tempi
- Prima il deterministico, l'agente solo come fallback
- L'orchestratore è codice: verifica, committa, non rilegge la sessione
- Contesti piccoli: a ogni agente solo la sua fetta, in JSON
- Punti saldi: il TDD e il double loop learning di Argyris

::: notes
Tutto ciò che si può risolvere senza un LLM si risolve senza un LLM. Chain of
responsibility: un handler deterministico risponde quando può, altrimenti
l'evento passa a un agente. Il vecchio orchestratore diventa una state
machine, che non consuma token. Gli agenti non committano: committa beck, dopo
aver verificato.

Il grafo è il punto di sincronizzazione: ogni agente riceve solo il nodo su
cui lavora. Input e output in JSON, ciascuno con il suo schema: un output
fuori schema è SignalRejected e il turno si ripete. Ogni agente scrive solo nel
suo scope (src/beck/agents/scopes.py): un turno che tocca un file fuori scope
viene annullato e rifiutato. I test si lanciano con `beck send test`: niente
pipeline bash improvvisate.

Cosa resta di minime: il TDD, che beck verifica a ogni fase invece di fidarsi
dell'agente, e il double loop learning di Argyris: un errore che si ripete corregge la
regola. beck non dipende da nulla di minime: ne riscrive le idee buone nei suoi
termini (docs/minime-agents.md).
:::

## Gli agenti di beck

- kent: lo scope, la shopping list, le decisioni
- hughes: il dominio
- ford: l'architettura e le interfacce
- unclebob: il TDD, una sessione per ciclo
- martin: il refactor e la review
- argyris: double loop learning; per ora un log

::: footer
Eliminati: joshua, marvin, linus, unclebob-pair · simon diventa una modalità operativa
:::

::: notes
Ogni comando è un turno nuovo con un contesto piccolo: unclebob ha una
sessione per ciclo (una sessione per unità era arrivata a 832k token per
turno). hughes e ford, fusi in kent il 13 novembre, sono tornati il 23
novembre: l'architettura ad agenti di minime era giusta. Di kent resta il
nome: non orchestra più. Le parole dell'utente diventano azioni attraverso
kent, scelte da un menu chiuso (RouteDecision).

Eliminati: joshua, sostituito dai controlli deterministici; marvin, perché gli
aggiornamenti di AGENTS.md si sono rivelati dannosi; linus, perché committa
beck; unclebob-pair, la versione interattiva di unclebob. simon merita una
menzione: torna come modalità operativa, in programma. Invece di implementare
codice, distilla la documentazione dal codice che c'è già, per capire un
sistema sconosciuto. argyris, l'agente che migliora gli agenti, per ora è un
log (.beck/argyris.log): il double loop learning lo chiude ancora l'utente.
:::

## L'architettura di beck

:::: {.columns}
::: {.column width="50%"}
**Il motore, deterministico**

- State machine, policy, scheduler
- Eventi: unica fonte di verità (CQRS ed event sourcing)
- Test, check, commit, squash, atterraggi
:::
::: {.column width="50%"}
**Intorno al motore**

- DSL: dominio e architettura
- Il grafo: dal requisito al test
- Un worktree per componente
- Agenti: comandi e risposte in JSON
:::
::::

::: notes
Il motore riceve un input (l'output di un agente, la risposta dell'utente),
registra i fatti come eventi e lancia le policy di ogni nuovo evento; le policy
comandano gli agenti o un'azione deterministica. Le DSL descrivono dominio e
architettura; il grafo lega ogni spec id al componente, ai simboli e ai test, e
da lì beck taglia la fetta per ciascun agente. I componenti avanzano in
parallelo, ognuno nel suo worktree, e atterrano sul branch verificati. Le
prossime slide entrano in ciascun pezzo.
:::

# Come funziona

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

- Due percentuali: i token e i minuti degli agenti di beck rispetto a minime
- Minuti: il modello che genera, senza comandi, test e attese
- Token pesati: input 1, scrittura in cache 1,25, lettura 0,1, output 5
- Le fonti: gli eventi di beck e i transcript di Claude Code
- Pesi uguali per tutti i modelli: si confronta il lavoro, non il prezzo

::: notes
100 % vuol dire quanto minime; sotto, beck consuma meno. I token di beck sono
negli eventi `AgentTurnEnded` dell'iterazione, quelli di minime nei transcript
di Claude Code, ogni messaggio contato una volta. Li somma `beck benchmark`:
niente è stimato, niente viene da una bolletta. I pesi sono quelli dell'analisi dei costi di minime. Le due fonti concordano (+0,7 % su
paddles, i turni falliti). Il tempo è quello degli agenti: i minuti in cui il
modello genera, dai transcript di Claude Code, con lo stesso calcolo per beck e
per minime. Restano fuori i comandi e i test, le attese dell'utente, i fermi, i
limiti d'uso e la rete; i minuti degli agenti in parallelo si sommano, come i
token. Modelli: minime tutto su Opus 5.5 (su hello joshua e simon su Sonnet
5.5; negli altri pilot joshua, simon e marvin erano spenti). Cosa i numeri non
dicono: come si guida un workflow pesa (su paddles il kent di minime faceva
polling sui suoi agenti); i token di chi guida il benchmark non sono in nessuno
dei due conti.
:::

## beck contro minime

:::: {.columns}
::: {.column width="50%"}
**Primo giro: kent su opus, unclebob e martin su sonnet**

- Token (% di minime): hello 6 %, pingpong 21 % e 17 %, paddles 6 %, alien-attack 34 %
- Minuti degli agenti (% di minime): hello 8 %, pingpong 22 % e 19 %, paddles 16 %, alien-attack 34 %
:::
::: {.column width="50%"}
**Rerun: tutti gli agenti su opus, come minime**

- Token (% di minime): hello 11 %, pingpong 36 %, paddles 14 %, alien-attack 70 %
- Minuti degli agenti (% di minime): hello 19 %, pingpong 54 %, paddles 49 %, alien-attack 84 %
:::
::::

::: notes
Percentuale di minime: 6 % di token vuol dire che beck ne ha spesi 6 ogni 100
di minime. Solo iterazioni complete. Pingpong nel primo giro ha due run
completi, il secondo e il terzo.

Token pesati, in milioni: hello minime 3,53, beck 0,20 e nel rerun 0,40;
pingpong 5,66, 1,17 e 0,95, rerun 2,03; paddles 57,10, 3,49, rerun 7,92;
alien-attack 26,80, 9,20, rerun 18,63.

Minuti degli agenti: hello minime 27,5, beck 2,2 e nel rerun 5,1; pingpong
46,9, 10,3 e 9,1, rerun 25,3; paddles 158,7, 25,7, rerun 77,9; alien-attack
152,9, 52,6, rerun 128,4.

Paddles è gonfiato dal polling del kent di minime. Togliendo kent da entrambi,
beck è al 25 % dei token e al 24 % dei minuti nel primo giro, al 59 % e
all'83 % nel rerun.

Alien-attack nel primo giro era al 18 % dei token alla prima review, con un
gioco ingiocabile (prossima slide); il 34 % comprende le correzioni. La
condizione di merge del branch: beck funziona e spende meno di minime.
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
- Il run pulito: 15 % dei token e 31 % dei minuti degli agenti di minime
- I bug trovati: coverage, formatter, unimplemented nascosti, grafo vecchio
- Gli agenti ora chiedono le parole ambigue e i testi che l'utente legge

::: notes
Hello world r2: il run pulito spende il 15 % dei token e il 31 % dei minuti
degli agenti di minime (8,4 contro 27,5), con tutti gli agenti su opus: kent,
hughes, ford, unclebob e martin. Esempi di fix: coverage.py leggeva i path come nomi di moduli; i
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
