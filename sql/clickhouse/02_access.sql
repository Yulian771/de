-- Техническая учётная запись ClickHouse

CREATE USER IF NOT EXISTS `$etl_user` IDENTIFIED WITH sha256_password BY '$etl_password';

ALTER USER `$etl_user` IDENTIFIED WITH sha256_password BY '$etl_password';

REVOKE ALL ON *.* FROM `$etl_user`;


GRANT SELECT, INSERT, ALTER ON `$db`.* TO `$etl_user`;

GRANT CREATE TABLE, DROP TABLE, SELECT, INSERT ON `$stage_db`.* TO `$etl_user`;


GRANT dictGet ON `$db`.* TO `$etl_user`;

GRANT SYSTEM RELOAD DICTIONARY ON *.* TO `$etl_user`;

GRANT CREATE TEMPORARY TABLE, POSTGRES ON *.* TO `$etl_user`;

GRANT NAMED COLLECTION ON pg_dwh TO `$etl_user`;
