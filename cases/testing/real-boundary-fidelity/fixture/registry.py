import sqlite3


class DuplicateUser(ValueError):
    pass


class UserRegistry:
    def __init__(self, connection):
        self._connection = connection
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, display_name TEXT NOT NULL)"
        )
        self._connection.commit()

    @staticmethod
    def _key(email):
        return email.strip().casefold()

    def add(self, email, display_name):
        try:
            self._connection.execute(
                "INSERT INTO users(email, display_name) VALUES (?, ?)",
                (self._key(email), display_name),
            )
            self._connection.commit()
        except sqlite3.IntegrityError as error:
            raise DuplicateUser(self._key(email)) from error

    def find(self, email):
        row = self._connection.execute(
            "SELECT email, display_name FROM users WHERE email = ?", (self._key(email),)
        ).fetchone()
        if row is None:
            return None
        return {"email": row[0], "display_name": row[1]}
