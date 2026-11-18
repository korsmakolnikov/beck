# Pilota 1 — hello world: proposte di refinement

Iterazione `5ca546dd`, ramo `hello-world-cli`, progetto in `~/code/pilots/hello` (copia di
`/tmp/pilots/hello`). Esito: `IterationClosed`, un componente (`greeter`), il programma funziona
(`hello` e `python -m hello` stampano `Hello, world!` ed escono con 0).

## I numeri

| | |
|---|---|
| Durata | 2026-11-17 09:55 → 22:44 UTC (quasi tutta attesa dei limiti d'uso) |
| Turni di kent | 13 (intervista, lista, dominio, architettura ×2, interfacce ×2, correzioni, 3 instradamenti) |
| Turni di unclebob | 7 (5 rossi, 2 verdi) |
| Turni di martin | 1.241, di cui **1.223** in un ciclo infinito di refactor (bug, corretto) |
| Decisioni dell'utente | 17 (7 domande di intervista, 3 approvazioni, 2 review, 3 "riprova" per il limite d'uso…) |
| Test finali | 11 per un programma che stampa una frase fissa |

## Cosa è andato storto, e cosa propongo

### 1. Il dominio è troppo fine: 4 requisiti per un solo comportamento

kent ha diviso "stampa `Hello, world!` ed esce con 0" in quattro requisiti:
`greeting-text-is-hello-world`, `greeting-is-written-on-stdout`, `greeting-exits-with-status-zero`,
`greeting-writes-nothing-on-stderr`. Un ciclo TDD per requisito: quattro test, quattro giri di
unclebob.

**La prova che era un comportamento solo**: 3 dei 4 rossi erano già verdi alla prima esecuzione
(`RedVerified` con `already_green`). Il primo verde li aveva già implementati tutti.

**Le conseguenze peggiori**: due di quei test erano deboli, e martin li ha segnalati con ragione:
`main` scrive su uno `StringIO` iniettato, quindi `capsys` non vede niente e il test su stderr
passerebbe anche se `main` scrivesse su stderr. Test piccoli e numerosi su un comportamento unico
diventano facilmente vacui.

**Proposte**

* *Nel prompt di kent*: un requisito è un comportamento osservabile che un test distinto può far
  fallire da solo. Le postcondizioni che falliscono sempre insieme sono un requisito solo. Il
  dominio è proporzionato al problema.
* *In beck, deterministico*: un rosso già verde perché l'ha implementato il verde di un altro
  requisito della stessa unità è il segnale di un requisito non distinto (lo stesso tipo di
  segnale della collisione di nomi per i domini). beck lo segnala nella review e propone a kent
  di fondere i requisiti; oltre una soglia (es. metà dei rossi dell'unità) lo chiede prima di
  andare avanti.

### 2. Una costante modellata come value object

`Greeting(text=...)` più `the_greeting()` per restituire una frase fissa: un'entità e una funzione
che non portano invarianti vere. È sovra-modellazione che genera interfacce, requisiti e test.

**Proposta** — *nel prompt di kent*: niente entità per una costante o per un valore senza
invarianti proprie; un'entità esiste quando ha almeno un'invariante che un test può far fallire.

### 3. I punti d'ingresso persi tra lista della spesa, dominio e interfacce

La lista della spesa chiedeva lo script `hello`, `python -m hello` e, in "Done when", un test che
lanci il programma come processo. Nel dominio e nelle interfacce non c'erano: ho dovuto rimandare
l'architettura a kent (mancavano `[project.scripts]` e `__main__.py`) e chiedere il test come
processo con una correzione in review. Senza un requisito, nessun ciclo li scrive.

**Proposta** — *tracciabilità deterministica*: ogni voce di "Deliver" e di "Done when" della lista
della spesa cita il requisito (o il morfismo) che la copre. `ValidateDomain` rifiuta una voce che
non ne cita nessuno. Il lancio del programma (script, `-m`) è il morfismo del componente
d'ingresso, con un requisito che si verifica lanciando il processo: un test solo, che copre anche
stdout, stderr e lo stato.

### 4. Le fitness function generate sono rumore su un componente solo

4 dei test sono fitness function generate: due "is-defined", le dipendenze dichiarate, e
un'asserzione di kent (`greeter-depends-on-no-system-module`), per un componente unico che non
dipende da niente.

**Proposta**: generare "is-defined" e "declared-dependencies" in un test solo, parametrizzato per
componente, e solo quando l'architettura ha più di un componente. Le asserzioni di kent restano.

### 5. Il ciclo infinito di martin (bug, già corretto: `b26ce83`)

martin, su un test debole, rispondeva `phase: none` nominando in `addressed` il requisito invece
del test; beck confrontava con l'id del test, non chiudeva mai il punto e glielo riconsegnava:
1.223 turni, circa 300 milioni di token in cache, la causa dei limiti d'uso continui.

**Fatto**: il punto chiuso è quello che beck ha consegnato, non quello che l'agente dichiara; lo
stesso punto consegnato più volte della regola di ripetizione blocca il refactor.

**Ancora da fare**: un tetto per componente (turni o token, dal `budget` della configurazione, oggi
non applicato) che fermi qualunque ciclo che le regole non prevedono.

### 6. Il limite d'uso ritentato tre volte (bug, già corretto: `065e80d`)

Un turno fallito per il limite delle 5 ore veniva ritentato tre volte prima di chiedere. Ora beck
chiede subito.

## Cosa ha funzionato

* L'intervista: domande pertinenti (launch form, tooling), una lista della spesa fedele.
* Il riavvio dell'architettura con una correzione: kent ha aggiunto i punti d'ingresso.
* Il nuovo core: i test di unclebob passano dal socket (`beck send test`), la ripresa dopo i
  limiti d'uso e dopo l'interruzione ha ripreso il turno di martin senza perdere niente.
* Il giro delle correzioni in review: kent ha pianificato, unclebob ha scritto il test come
  processo, martin ha corretto due punti (`out or sys.stdout`, un import duplicato), i fixup
  sono stati riuniti nel commit del componente.
