-- One Postgres instance, two databases.
-- POSTGRES_DB creates `payments`; the merchant needs its own alongside it.
-- Runs only on FIRST initialization of an empty data volume.
CREATE DATABASE merchant;
