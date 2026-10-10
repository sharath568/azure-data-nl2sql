-- nl2sql: create the sample database on SQL Server (not needed on Azure SQL
-- Database, where infra/main.bicep creates it). Run as an admin, connected to master.

IF DB_ID(N'nl2sql_sample') IS NULL
    CREATE DATABASE nl2sql_sample;
GO
