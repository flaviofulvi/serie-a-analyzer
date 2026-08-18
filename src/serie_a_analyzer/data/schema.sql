-- Serie A Analyzer — schema iniziale (Postgres / Supabase)
--
-- Principi di progetto:
--
--  1. Nessun hard-coding "solo Serie A": esiste una tabella `leagues` e tutto
--     pende da li'. Non aggiunge complessita' oggi e non chiude la porta al
--     multi-campionato domani.
--  2. I pronostici sono IMMUTABILI e legati a una versione del modello. E' la
--     regola piu' importante di tutto lo schema: se un pronostico potesse
--     essere riscritto dopo aver visto il risultato, lo storico di accuratezza
--     varrebbe zero. Si inserisce, non si aggiorna mai.
--  3. Le quote sono un'entita' di prima classe: sono il metro di paragone
--     gratuito per capire quanto vale il modello.
--  4. Ogni riga che viene da API-Football conserva il suo id di origine, per
--     rendere l'ingestione idempotente (ri-eseguirla non duplica nulla).

-- ============================================================================
-- ANAGRAFICHE
-- ============================================================================

create table if not exists leagues (
    id              bigserial primary key,
    api_id          integer unique not null,
    name            text not null,
    country         text not null,
    created_at      timestamptz not null default now()
);

create table if not exists seasons (
    id              bigserial primary key,
    league_id       bigint not null references leagues(id) on delete cascade,
    year            integer not null,          -- anno di inizio: 2025 = 2025/26
    start_date      date,
    end_date        date,
    is_current      boolean not null default false,
    unique (league_id, year)
);

create table if not exists teams (
    id              bigserial primary key,
    api_id          integer unique not null,
    name            text not null,
    short_name      text,
    founded         integer,
    venue_name      text,
    venue_city      text,
    created_at      timestamptz not null default now()
);

create table if not exists players (
    id              bigserial primary key,
    api_id          integer unique not null,
    name            text not null,
    firstname       text,
    lastname        text,
    birth_date      date,
    nationality     text,
    height_cm       integer,                   -- normalizzato da "180 cm"
    weight_kg       integer,
    -- Il piede preferito spesso non e' esposto da API-Football: la colonna
    -- resta nullable e la scheda giocatore deve reggere il valore mancante.
    preferred_foot  text check (preferred_foot in ('destro', 'sinistro', 'ambidestro')),
    position        text,
    created_at      timestamptz not null default now()
);

-- Storico trasferimenti: un giocatore appartiene a una squadra in un intervallo.
-- `to_date` null = militanza in corso.
create table if not exists player_team_spells (
    id              bigserial primary key,
    player_id       bigint not null references players(id) on delete cascade,
    team_id         bigint not null references teams(id) on delete cascade,
    from_date       date not null,
    to_date         date,
    transfer_type   text,                      -- 'prestito', 'definitivo', 'svincolato'...
    source          text,                      -- provenienza del dato, per il debug
    unique (player_id, team_id, from_date)
);

create index if not exists idx_spells_player on player_team_spells(player_id, from_date desc);

-- Indisponibilita': infortuni e squalifiche. Pesano molto sui pronostici
-- "secchi", quelli generati prima che escano le formazioni ufficiali.
create table if not exists player_availability (
    id              bigserial primary key,
    player_id       bigint not null references players(id) on delete cascade,
    team_id         bigint references teams(id) on delete set null,
    kind            text not null check (kind in ('infortunio', 'squalifica', 'altro')),
    description     text,
    from_date       date not null,
    to_date         date,
    created_at      timestamptz not null default now()
);

create index if not exists idx_availability_player on player_availability(player_id, from_date desc);

-- ============================================================================
-- PARTITE
-- ============================================================================

create table if not exists fixtures (
    id              bigserial primary key,
    api_id          integer unique not null,
    season_id       bigint not null references seasons(id) on delete cascade,
    round           text,                      -- "Regular Season - 3"
    matchday        integer,                   -- estratto da round, per ordinare
    home_team_id    bigint not null references teams(id),
    away_team_id    bigint not null references teams(id),
    kickoff_utc     timestamptz not null,
    -- Serie A rinvia e recupera partite: lo stato governa tutta la pipeline,
    -- che lavora sulla singola partita conclusa e non sulla "giornata" come
    -- blocco (una giornata puo' restare aperta per mesi).
    status          text not null default 'scheduled'
                    check (status in ('scheduled', 'live', 'finished', 'postponed', 'cancelled')),
    home_goals      integer,
    away_goals      integer,
    home_goals_ht   integer,
    away_goals_ht   integer,
    updated_at      timestamptz not null default now(),
    check (home_team_id <> away_team_id)
);

create index if not exists idx_fixtures_season_matchday on fixtures(season_id, matchday);
create index if not exists idx_fixtures_kickoff on fixtures(kickoff_utc);
create index if not exists idx_fixtures_status on fixtures(status) where status <> 'finished';

create table if not exists team_match_stats (
    fixture_id      bigint not null references fixtures(id) on delete cascade,
    team_id         bigint not null references teams(id),
    shots           integer,
    shots_on_target integer,
    possession_pct  numeric(5, 2),
    corners         integer,
    fouls           integer,
    yellow_cards    integer,
    red_cards       integer,
    expected_goals  numeric(6, 3),             -- non sempre disponibile
    primary key (fixture_id, team_id)
);

-- ============================================================================
-- FORMAZIONI
-- ============================================================================

create table if not exists lineups (
    id              bigserial primary key,
    fixture_id      bigint not null references fixtures(id) on delete cascade,
    team_id         bigint not null references teams(id),
    formation       text,                      -- "4-3-3"
    -- Distingue le probabili formazioni da quelle ufficiali pubblicate a circa
    -- mezz'ora dal calcio d'inizio: e' cio' che separa le due modalita' di
    -- pronostico.
    is_official     boolean not null default false,
    published_at    timestamptz not null default now(),
    unique (fixture_id, team_id, is_official)
);

create table if not exists lineup_players (
    lineup_id       bigint not null references lineups(id) on delete cascade,
    player_id       bigint not null references players(id),
    is_starter      boolean not null,
    shirt_number    integer,
    position        text,
    grid            text,                      -- posizione in campo "4:2"
    primary key (lineup_id, player_id)
);

create table if not exists player_match_stats (
    fixture_id      bigint not null references fixtures(id) on delete cascade,
    player_id       bigint not null references players(id),
    team_id         bigint not null references teams(id),
    minutes         integer not null default 0,
    goals           integer not null default 0,
    assists         integer not null default 0,
    shots           integer,
    shots_on_target integer,
    yellow_cards    integer not null default 0,
    red_cards       integer not null default 0,
    expected_goals  numeric(6, 3),
    rating          numeric(4, 2),
    primary key (fixture_id, player_id)
);

create index if not exists idx_pms_player on player_match_stats(player_id);

-- ============================================================================
-- QUOTE — il metro di paragone
-- ============================================================================

create table if not exists odds (
    id              bigserial primary key,
    fixture_id      bigint not null references fixtures(id) on delete cascade,
    bookmaker       text not null,
    market          text not null,             -- '1x2', 'over_under', 'btts'...
    selection       text not null,             -- 'home', 'over_2.5', 'yes'...
    decimal_odds    numeric(8, 3) not null check (decimal_odds > 1),
    collected_at    timestamptz not null default now(),
    -- Le quote di chiusura sono le piu' accurate: sono il vero benchmark.
    is_closing      boolean not null default false
);

create index if not exists idx_odds_fixture on odds(fixture_id, market, collected_at desc);

-- ============================================================================
-- MODELLO E PRONOSTICI
-- ============================================================================

-- Ogni stima del modello e' una riga: parametri congelati e riproducibili.
create table if not exists model_versions (
    id              bigserial primary key,
    name            text not null,             -- 'dixon_coles'
    version         text not null,             -- '0.1.0'
    fitted_at       timestamptz not null default now(),
    -- Iperparametri usati (emivita, shrinkage, finestra dati...).
    config          jsonb not null default '{}'::jsonb,
    -- Forze stimate per squadra, fattore campo, base, rho.
    params          jsonb not null,
    n_matches       integer,
    notes           text,
    unique (name, version, fitted_at)
);

-- Un pronostico e' un evento immutabile: si inserisce e non si tocca piu'.
-- Rigenerare un pronostico significa inserire una NUOVA riga, non aggiornare
-- quella vecchia. E' cio' che rende onesto il tracking di accuratezza.
create table if not exists predictions (
    id                bigserial primary key,
    fixture_id        bigint not null references fixtures(id) on delete cascade,
    model_version_id  bigint not null references model_versions(id),
    mode              text not null check (mode in ('secco', 'formazione_ufficiale')),
    created_at        timestamptz not null default now(),
    lambda_home       numeric(6, 3) not null,
    lambda_away       numeric(6, 3) not null,
    rho               numeric(6, 4) not null,
    -- La matrice dei risultati serializzata: da questa si riderivano tutti i
    -- mercati anche a distanza di mesi, esattamente come erano allora.
    scoreline         jsonb not null,
    unique (fixture_id, model_version_id, mode, created_at)
);

create index if not exists idx_predictions_fixture on predictions(fixture_id, created_at desc);

-- Esiti derivati, materializzati per non ricalcolarli a ogni lettura.
create table if not exists prediction_markets (
    id              bigserial primary key,
    prediction_id   bigint not null references predictions(id) on delete cascade,
    market          text not null,             -- '1x2', 'over_under', 'marcatore'...
    selection       text not null,             -- 'home', 'over_2.5', 'player:278'...
    probability     numeric(7, 6) not null check (probability between 0 and 1),
    unique (prediction_id, market, selection)
);

create index if not exists idx_pmarkets_lookup on prediction_markets(prediction_id, market);

-- Il collegamento pronostico <-> realta'. Popolato dalla pipeline di fine
-- partita, e' la base dell'indicatore di precisione in home.
create table if not exists prediction_outcomes (
    prediction_market_id bigint primary key references prediction_markets(id) on delete cascade,
    -- Se l'esito pronosticato si e' verificato.
    hit                  boolean not null,
    -- Contributo al Brier score: (probabilita' - esito)^2. La "percentuale di
    -- azzeccate" da sola non dice nulla su un pronostico probabilistico.
    brier                numeric(8, 6) not null,
    resolved_at          timestamptz not null default now()
);

-- ============================================================================
-- VISTE DI COMODO
-- ============================================================================

-- Il pronostico piu' recente per ogni partita e modalita'.
create or replace view latest_predictions as
select distinct on (fixture_id, mode) *
from predictions
order by fixture_id, mode, created_at desc;

-- Calibrazione aggregata: quanto valgono davvero i pronostici.
create or replace view accuracy_summary as
select
    pm.market,
    count(*)                              as n,
    avg(po.brier)                         as brier_medio,
    avg(case when po.hit then 1.0 else 0.0 end) as tasso_realizzazione,
    avg(pm.probability)                   as probabilita_media
from prediction_outcomes po
join prediction_markets pm on pm.id = po.prediction_market_id
group by pm.market;
