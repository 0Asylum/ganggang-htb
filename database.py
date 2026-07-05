import sqlite3
import logging

logger = logging.getLogger(__name__)

SHERLOCK_CATEGORIES = {
    "DFIR", "Cloud", "Malware Analysis", "SOC", "Threat Intelligence"
}


class DatabaseError(Exception):
    """Raised for any database init/connection failure."""
    pass


def clean_avatar_url(avatar_url):
    """HTB's team/activity feed sometimes reports a generic placeholder thumbnail
    (e.g. machine_default_thumb.png) instead of the real box/challenge art -- seen
    happening for older/retired objects. Treated as equivalent to "no avatar" so it
    never overwrites a real avatar_url already on file (see upsert_machine/
    upsert_challenge/upsert_sherlock's COALESCE-on-conflict, which only protects
    against NULL, not a worse-but-non-NULL value like this).
    """
    if avatar_url and "_default_thumb" in avatar_url:
        return None
    return avatar_url


class Database:
    """SQLite-backed storage for everything the bot tracks: HTB users, machines,
    challenges, sherlocks, pro labs, fortresses, each user's solve/progress
    history, Discord account links, and pending claims.
    """

    def __init__(self, filename):
        """Opens (creating if needed) the SQLite file at `filename` and
        ensures all tables exist.
        """
        self.filename = filename
        self._connection = sqlite3.connect(self.filename)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_tables()

    def is_empty(self):
        """True if team_activity has no rows yet, i.e. this is a fresh database
        that still needs its initial seed from HTB's activity feed.
        """
        cursor = self._connection.execute("SELECT COUNT(*) FROM team_activity")
        return cursor.fetchone()[0] == 0

    def _create_tables(self):
        """Creates every table if it doesn't already exist, then applies any
        column migrations needed for databases created before those columns
        existed.
        """
        stmts = [
            """
            CREATE TABLE IF NOT EXISTS team_activity (
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                user_avatar_url TEXT,
                object_id INTEGER NOT NULL,
                object_name TEXT NOT NULL,
                object_type TEXT NOT NULL,
                type TEXT NOT NULL,
                points INTEGER NOT NULL,
                first_blood INTEGER NOT NULL,
                category TEXT,
                object_avatar_url TEXT,
                date TEXT NOT NULL,
                UNIQUE(user_id, object_id, type)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                avatar_url TEXT,
                rank TEXT,
                rank_id INTEGER,
                country_code TEXT,
                points INTEGER,
                system_owns INTEGER DEFAULT 0,
                user_owns INTEGER DEFAULT 0,
                system_bloods INTEGER DEFAULT 0,
                user_bloods INTEGER DEFAULT 0,
                challenge_owns INTEGER DEFAULT 0,
                challenge_bloods INTEGER DEFAULT 0,
                last_synced_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS machines (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                avatar_url TEXT,
                os TEXT,
                difficulty TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS challenges (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                avatar_url TEXT,
                category TEXT,
                difficulty TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sherlocks (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                avatar_url TEXT,
                category TEXT,
                difficulty TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS prolabs (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                avatar_url TEXT,
                identifier TEXT,
                total_flags INTEGER DEFAULT 0,
                total_machines INTEGER DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS fortresses (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                avatar_url TEXT,
                total_flags INTEGER DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_machine_solves (
                user_id INTEGER NOT NULL,
                machine_id INTEGER NOT NULL,
                solve_type TEXT NOT NULL,
                own_date TEXT NOT NULL,
                blood INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0,
                team_blood INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, machine_id, solve_type)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_challenge_solves (
                user_id INTEGER NOT NULL,
                challenge_id INTEGER NOT NULL,
                own_date TEXT NOT NULL,
                blood INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0,
                team_blood INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, challenge_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_sherlock_solves (
                user_id INTEGER NOT NULL,
                sherlock_id INTEGER NOT NULL,
                own_date TEXT NOT NULL,
                blood INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0,
                team_blood INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, sherlock_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_prolab_progress (
                user_id INTEGER NOT NULL,
                prolab_id INTEGER NOT NULL,
                owned_flags INTEGER DEFAULT 0,
                completion_percentage REAL DEFAULT 0,
                PRIMARY KEY (user_id, prolab_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_fortress_progress (
                user_id INTEGER NOT NULL,
                fortress_id INTEGER NOT NULL,
                owned_flags INTEGER DEFAULT 0,
                completion_percentage REAL DEFAULT 0,
                PRIMARY KEY (user_id, fortress_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS discord_users (
                discord_id INTEGER PRIMARY KEY,
                role INTEGER NOT NULL DEFAULT 0,
                htb_user_id INTEGER REFERENCES users(id),
                avatar_pref TEXT NOT NULL DEFAULT 'htb',
                tag TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER NOT NULL,
                htb_user_id INTEGER NOT NULL REFERENCES users(id),
                status TEXT NOT NULL DEFAULT 'pending',
                requested_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by INTEGER
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS challenge_categories (
                name TEXT PRIMARY KEY,
                icon_url TEXT NOT NULL
            )
            """,
        ]
        for stmt in stmts:
            self._connection.execute(stmt)

        # CREATE TABLE IF NOT EXISTS doesn't add columns to a table that already
        # exists from before this column was introduced -- migrate it in manually.
        existing_cols = {row["name"] for row in self._connection.execute("PRAGMA table_info(discord_users)")}
        if "avatar_pref" not in existing_cols:
            self._connection.execute(
                "ALTER TABLE discord_users ADD COLUMN avatar_pref TEXT NOT NULL DEFAULT 'htb'"
            )
        if "tag" not in existing_cols:
            self._connection.execute("ALTER TABLE discord_users ADD COLUMN tag TEXT")

        # Hard DB-level guarantee that an HTB profile can only ever be linked to one
        # Discord account -- a partial index (ignoring NULLs) so unclaimed rows don't
        # collide with each other. The one-discord-account-per-HTB-profile direction
        # is already guaranteed by discord_id being the primary key.
        self._connection.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_discord_users_htb_user_id
            ON discord_users(htb_user_id) WHERE htb_user_id IS NOT NULL
        """)

        self._connection.commit()

    # ── team_activity ──────────────────────────────────────────────────────────

    def insert_activity(self, entry):
        """Inserts one team_activity row if it isn't already present (a solve
        is uniquely identified by user + object + type). Returns True if a new
        row was actually inserted, False if it was a duplicate.
        """
        cursor = self._connection.execute("""
            INSERT OR IGNORE INTO team_activity
                (user_id, user_name, user_avatar_url, object_id, object_name,
                 object_type, type, points, first_blood, category, object_avatar_url, date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry["user_id"],
            entry["user_name"],
            entry.get("user_avatar_url"),
            entry["object_id"],
            entry["object_name"],
            entry["object_type"],
            entry["type"],
            entry["points"],
            1 if entry["first_blood"] else 0,
            entry.get("category"),
            entry.get("object_avatar_url"),
            entry["date"],
        ))
        self._connection.commit()
        return cursor.rowcount > 0

    # ── users ──────────────────────────────────────────────────────────────────

    def user_synced(self, user_id):
        """True if this HTB user has ever completed a full profile sync."""
        cursor = self._connection.execute(
            "SELECT last_synced_at FROM users WHERE id = ?", (user_id,)
        )
        row = cursor.fetchone()
        return row is not None and row["last_synced_at"] is not None

    def upsert_user(self, profile):
        """Inserts or updates a user's basic HTB profile fields (name, avatar,
        rank, points, lifetime blood/own counts).
        """
        self._connection.execute("""
            INSERT INTO users
                (id, name, avatar_url, rank, rank_id, country_code, points,
                 system_owns, user_owns, system_bloods, user_bloods,
                 challenge_owns, challenge_bloods)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                avatar_url=excluded.avatar_url,
                rank=excluded.rank,
                rank_id=excluded.rank_id,
                country_code=excluded.country_code,
                points=excluded.points,
                system_owns=excluded.system_owns,
                user_owns=excluded.user_owns,
                system_bloods=excluded.system_bloods,
                user_bloods=excluded.user_bloods,
                challenge_owns=excluded.challenge_owns,
                challenge_bloods=excluded.challenge_bloods
        """, (
            profile["id"],
            profile["name"],
            profile.get("avatar"),
            profile.get("rank"),
            profile.get("rank_id"),
            profile.get("country_code"),
            profile.get("points"),
            profile.get("system_owns", 0),
            profile.get("user_owns", 0),
            profile.get("system_bloods", 0),
            profile.get("user_bloods", 0),
            profile.get("challenge_owns", 0),
            profile.get("challenge_bloods", 0),
        ))
        self._connection.commit()

    def mark_user_synced(self, user_id, timestamp):
        """Records when a user's full profile sync last completed."""
        self._connection.execute(
            "UPDATE users SET last_synced_at = ? WHERE id = ?",
            (timestamp, user_id),
        )
        self._connection.commit()

    def get_user(self, user_id):
        """Fetches a user row by HTB user ID, or None if not found."""
        cursor = self._connection.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        )
        return cursor.fetchone()

    def get_user_by_name(self, name):
        """Fetches a user row by HTB username (case-insensitive), or None."""
        cursor = self._connection.execute(
            "SELECT * FROM users WHERE name = ? COLLATE NOCASE", (name,)
        )
        return cursor.fetchone()

    # ── entity upserts ─────────────────────────────────────────────────────────

    def upsert_machine(self, machine_id, name, avatar_url=None, os=None, difficulty=None):
        """Inserts or updates a machine's name/avatar/os/difficulty. A falsy
        field on update never overwrites an existing non-null value (COALESCE),
        so a partial update from one data source doesn't erase data another
        source already filled in.
        """
        avatar_url = clean_avatar_url(avatar_url)
        self._connection.execute("""
            INSERT INTO machines (id, name, avatar_url, os, difficulty)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                avatar_url=COALESCE(excluded.avatar_url, machines.avatar_url),
                os=COALESCE(excluded.os, machines.os),
                difficulty=COALESCE(excluded.difficulty, machines.difficulty)
        """, (machine_id, name, avatar_url, os, difficulty))

    def upsert_challenge(self, challenge_id, name, avatar_url=None, category=None):
        """Inserts or updates a challenge's name/avatar/category. Same
        COALESCE-on-update behavior as upsert_machine.
        """
        avatar_url = clean_avatar_url(avatar_url)
        self._connection.execute("""
            INSERT INTO challenges (id, name, avatar_url, category)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                avatar_url=COALESCE(excluded.avatar_url, challenges.avatar_url),
                category=COALESCE(excluded.category, challenges.category)
        """, (challenge_id, name, avatar_url, category))

    def get_challenge_category_icon(self, category):
        """Looks up a challenge category's icon URL from our own record of HTB's
        challenge/categories/list -- separate from challenges.avatar_url because
        team/activity never reports a per-solve avatar_url for challenges at all
        (see poller._resolve_challenge_avatar), so this is the fallback source.
        """
        if not category:
            return None
        row = self._connection.execute(
            "SELECT icon_url FROM challenge_categories WHERE name = ?", (category,)
        ).fetchone()
        return row["icon_url"] if row else None

    def upsert_challenge_categories(self, categories):
        """Bulk-upserts {name: icon_url} pairs, e.g. from HTB's
        challenge/categories/list. Called only when a category we don't already
        have on file shows up in a solve, so this stays a rare, on-demand call
        rather than a recurring poll.
        """
        self._connection.executemany("""
            INSERT INTO challenge_categories (name, icon_url)
            VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET icon_url=excluded.icon_url
        """, list(categories.items()))

    def upsert_sherlock(self, sherlock_id, name, avatar_url=None, category=None):
        """Inserts or updates a sherlock's name/avatar/category. Same
        COALESCE-on-update behavior as upsert_machine.
        """
        avatar_url = clean_avatar_url(avatar_url)
        self._connection.execute("""
            INSERT INTO sherlocks (id, name, avatar_url, category)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                avatar_url=COALESCE(excluded.avatar_url, sherlocks.avatar_url),
                category=COALESCE(excluded.category, sherlocks.category)
        """, (sherlock_id, name, avatar_url, category))

    def upsert_prolab(self, prolab_id, name, avatar_url=None, identifier=None,
                      total_flags=0, total_machines=0):
        """Inserts or updates a pro lab's name/avatar/identifier/flag counts."""
        avatar_url = clean_avatar_url(avatar_url)
        self._connection.execute("""
            INSERT INTO prolabs (id, name, avatar_url, identifier, total_flags, total_machines)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                avatar_url=COALESCE(excluded.avatar_url, prolabs.avatar_url),
                identifier=COALESCE(excluded.identifier, prolabs.identifier),
                total_flags=excluded.total_flags,
                total_machines=excluded.total_machines
        """, (prolab_id, name, avatar_url, identifier, total_flags, total_machines))

    def upsert_fortress(self, fortress_id, name, avatar_url=None, total_flags=0):
        """Inserts or updates a fortress's name/avatar/flag count."""
        avatar_url = clean_avatar_url(avatar_url)
        self._connection.execute("""
            INSERT INTO fortresses (id, name, avatar_url, total_flags)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                avatar_url=COALESCE(excluded.avatar_url, fortresses.avatar_url),
                total_flags=excluded.total_flags
        """, (fortress_id, name, avatar_url, total_flags))

    # ── solve / progress upserts ───────────────────────────────────────────────

    def upsert_machine_solve(self, user_id, machine_id, solve_type, own_date,
                             blood=False, points=0):
        """Records a user's user/root solve on a machine, and works out
        whether they hold team blood for it: the earliest solver by own_date
        keeps it, so an out-of-order sync (e.g. discovering an older solve
        after a newer one was already recorded) correctly reassigns it.
        """
        holder = self._connection.execute("""
            SELECT user_id, own_date FROM user_machine_solves
            WHERE machine_id = ? AND solve_type = ? AND team_blood = 1
        """, (machine_id, solve_type)).fetchone()

        if holder is None:
            team_blood = 1
        elif own_date < holder["own_date"]:
            self._connection.execute("""
                UPDATE user_machine_solves SET team_blood = 0
                WHERE machine_id = ? AND solve_type = ? AND team_blood = 1
            """, (machine_id, solve_type))
            team_blood = 1
        else:
            team_blood = 0

        self._connection.execute("""
            INSERT INTO user_machine_solves
                (user_id, machine_id, solve_type, own_date, blood, points, team_blood)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, machine_id, solve_type) DO UPDATE SET
                own_date=excluded.own_date,
                blood=excluded.blood,
                points=excluded.points,
                team_blood=excluded.team_blood
        """, (user_id, machine_id, solve_type, own_date, 1 if blood else 0, points, team_blood))

    def upsert_challenge_solve(self, user_id, challenge_id, own_date,
                               blood=False, points=0):
        """Records a user's challenge solve and works out team blood, same
        earliest-by-own_date rule as upsert_machine_solve.
        """
        holder = self._connection.execute("""
            SELECT user_id, own_date FROM user_challenge_solves
            WHERE challenge_id = ? AND team_blood = 1
        """, (challenge_id,)).fetchone()

        if holder is None:
            team_blood = 1
        elif own_date < holder["own_date"]:
            self._connection.execute("""
                UPDATE user_challenge_solves SET team_blood = 0
                WHERE challenge_id = ? AND team_blood = 1
            """, (challenge_id,))
            team_blood = 1
        else:
            team_blood = 0

        self._connection.execute("""
            INSERT INTO user_challenge_solves
                (user_id, challenge_id, own_date, blood, points, team_blood)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, challenge_id) DO UPDATE SET
                own_date=excluded.own_date,
                blood=excluded.blood,
                points=excluded.points,
                team_blood=excluded.team_blood
        """, (user_id, challenge_id, own_date, 1 if blood else 0, points, team_blood))

    def upsert_sherlock_solve(self, user_id, sherlock_id, own_date,
                              blood=False, points=0):
        """Records a user's sherlock solve and works out team blood, same
        earliest-by-own_date rule as upsert_machine_solve.
        """
        holder = self._connection.execute("""
            SELECT user_id, own_date FROM user_sherlock_solves
            WHERE sherlock_id = ? AND team_blood = 1
        """, (sherlock_id,)).fetchone()

        if holder is None:
            team_blood = 1
        elif own_date < holder["own_date"]:
            self._connection.execute("""
                UPDATE user_sherlock_solves SET team_blood = 0
                WHERE sherlock_id = ? AND team_blood = 1
            """, (sherlock_id,))
            team_blood = 1
        else:
            team_blood = 0

        self._connection.execute("""
            INSERT INTO user_sherlock_solves
                (user_id, sherlock_id, own_date, blood, points, team_blood)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, sherlock_id) DO UPDATE SET
                own_date=excluded.own_date,
                blood=excluded.blood,
                points=excluded.points,
                team_blood=excluded.team_blood
        """, (user_id, sherlock_id, own_date, 1 if blood else 0, points, team_blood))

    def upsert_prolab_progress(self, user_id, prolab_id, owned_flags,
                               completion_percentage):
        """Records a user's flag progress on a pro lab."""
        self._connection.execute("""
            INSERT INTO user_prolab_progress
                (user_id, prolab_id, owned_flags, completion_percentage)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, prolab_id) DO UPDATE SET
                owned_flags=excluded.owned_flags,
                completion_percentage=excluded.completion_percentage
        """, (user_id, prolab_id, owned_flags, completion_percentage))

    def upsert_fortress_progress(self, user_id, fortress_id, owned_flags,
                                 completion_percentage):
        """Records a user's flag progress on a fortress."""
        self._connection.execute("""
            INSERT INTO user_fortress_progress
                (user_id, fortress_id, owned_flags, completion_percentage)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, fortress_id) DO UPDATE SET
                owned_flags=excluded.owned_flags,
                completion_percentage=excluded.completion_percentage
        """, (user_id, fortress_id, owned_flags, completion_percentage))

    def is_team_blood(self, object_id, object_type, solve_type, user_id, date, category=None):
        """Whether user_id currently holds (or would hold) the team blood for this
        object, checked against the authoritative holder tables (user_machine_solves /
        user_challenge_solves / user_sherlock_solves -- populated by full profile
        syncs), not team_activity.

        team_activity is only a rolling window of what the bot has itself observed
        live (bounded by HTB's 90-day activity feed), so it can be missing a
        teammate's real, older first solve -- e.g. one from years before this bot
        ever started polling. Checking it directly caused false "team blood"
        announcements for solves that were actually long since taken. The holder
        tables don't have that gap: they're built from each user's full lifetime
        activity history on their first sync.
        """
        if object_type == "machine":
            holder = self._connection.execute("""
                SELECT user_id, own_date FROM user_machine_solves
                WHERE machine_id = ? AND solve_type = ? AND team_blood = 1
            """, (object_id, solve_type)).fetchone()
        elif category in SHERLOCK_CATEGORIES:
            holder = self._connection.execute("""
                SELECT user_id, own_date FROM user_sherlock_solves
                WHERE sherlock_id = ? AND team_blood = 1
            """, (object_id,)).fetchone()
        else:
            holder = self._connection.execute("""
                SELECT user_id, own_date FROM user_challenge_solves
                WHERE challenge_id = ? AND team_blood = 1
            """, (object_id,)).fetchone()

        if holder is None or holder["user_id"] == user_id:
            return True
        return date < holder["own_date"]

    def commit(self):
        """Commits the current transaction."""
        self._connection.commit()

    # ── queries ────────────────────────────────────────────────────────────────

    def get_user_stats(self, user_id):
        """Gathers everything !stats needs for one user: machine/challenge/
        sherlock solve and blood counts, plus in-progress pro labs and
        fortresses.
        """
        machine_user = self._connection.execute(
            "SELECT COUNT(*) FROM user_machine_solves WHERE user_id=? AND solve_type='user'",
            (user_id,),
        ).fetchone()[0]
        machine_root = self._connection.execute(
            "SELECT COUNT(*) FROM user_machine_solves WHERE user_id=? AND solve_type='root'",
            (user_id,),
        ).fetchone()[0]
        machine_bloods = self._connection.execute(
            "SELECT COUNT(*) FROM user_machine_solves WHERE user_id=? AND blood=1",
            (user_id,),
        ).fetchone()[0]
        machine_team_bloods = self._connection.execute(
            "SELECT COUNT(*) FROM user_machine_solves WHERE user_id=? AND team_blood=1",
            (user_id,),
        ).fetchone()[0]
        challenges = self._connection.execute(
            "SELECT COUNT(*) FROM user_challenge_solves WHERE user_id=?",
            (user_id,),
        ).fetchone()[0]
        challenge_bloods = self._connection.execute(
            "SELECT COUNT(*) FROM user_challenge_solves WHERE user_id=? AND blood=1",
            (user_id,),
        ).fetchone()[0]
        challenge_team_bloods = self._connection.execute(
            "SELECT COUNT(*) FROM user_challenge_solves WHERE user_id=? AND team_blood=1",
            (user_id,),
        ).fetchone()[0]
        sherlocks = self._connection.execute(
            "SELECT COUNT(*) FROM user_sherlock_solves WHERE user_id=?",
            (user_id,),
        ).fetchone()[0]
        sherlock_bloods = self._connection.execute(
            "SELECT COUNT(*) FROM user_sherlock_solves WHERE user_id=? AND blood=1",
            (user_id,),
        ).fetchone()[0]
        sherlock_team_bloods = self._connection.execute(
            "SELECT COUNT(*) FROM user_sherlock_solves WHERE user_id=? AND team_blood=1",
            (user_id,),
        ).fetchone()[0]
        prolabs = self._connection.execute("""
            SELECT p.name, p.identifier, p.total_flags, up.owned_flags, up.completion_percentage
            FROM user_prolab_progress up
            JOIN prolabs p ON p.id = up.prolab_id
            WHERE up.user_id = ? AND up.owned_flags > 0
            ORDER BY up.completion_percentage DESC
        """, (user_id,)).fetchall()
        fortresses = self._connection.execute("""
            SELECT f.name, f.total_flags, uf.owned_flags, uf.completion_percentage
            FROM user_fortress_progress uf
            JOIN fortresses f ON f.id = uf.fortress_id
            WHERE uf.user_id = ? AND uf.owned_flags > 0
            ORDER BY uf.completion_percentage DESC
        """, (user_id,)).fetchall()

        return {
            "machine_user": machine_user,
            "machine_root": machine_root,
            "machine_bloods": machine_bloods,
            "machine_team_bloods": machine_team_bloods,
            "challenges": challenges,
            "challenge_bloods": challenge_bloods,
            "challenge_team_bloods": challenge_team_bloods,
            "sherlocks": sherlocks,
            "sherlock_bloods": sherlock_bloods,
            "sherlock_team_bloods": sherlock_team_bloods,
            "prolabs": [dict(r) for r in prolabs],
            "fortresses": [dict(r) for r in fortresses],
        }

    def get_leaderboard_points(self, limit=10):
        """Top `limit` users by total HTB points."""
        cursor = self._connection.execute("""
            SELECT id, name, avatar_url, points FROM users
            WHERE points IS NOT NULL
            ORDER BY points DESC
            LIMIT ?
        """, (limit,))
        return cursor.fetchall()

    def get_leaderboard_team_bloods(self, since, limit=10):
        """Top `limit` users by team-blood count across machines, challenges,
        and sherlocks, counting only bloods on or after `since`.
        """
        cursor = self._connection.execute("""
            SELECT u.id, u.name, u.avatar_url, COUNT(*) AS blood_count
            FROM (
                SELECT user_id, own_date FROM user_machine_solves WHERE team_blood = 1
                UNION ALL
                SELECT user_id, own_date FROM user_challenge_solves WHERE team_blood = 1
                UNION ALL
                SELECT user_id, own_date FROM user_sherlock_solves WHERE team_blood = 1
            ) b
            JOIN users u ON u.id = b.user_id
            WHERE b.own_date >= ?
            GROUP BY u.id, u.name, u.avatar_url
            ORDER BY blood_count DESC
            LIMIT ?
        """, (since, limit))
        return cursor.fetchall()

    def get_leaderboard_season_bloods(self, machine_ids, limit=10):
        """Top `limit` users by team-blood count, restricted to the given set
        of machine IDs (the current season's machines).
        """
        machine_ids = list(machine_ids)
        if not machine_ids:
            return []
        placeholders = ",".join("?" * len(machine_ids))
        cursor = self._connection.execute(f"""
            SELECT u.id, u.name, u.avatar_url, COUNT(*) AS blood_count
            FROM user_machine_solves s
            JOIN users u ON u.id = s.user_id
            WHERE s.team_blood = 1 AND s.machine_id IN ({placeholders})
            GROUP BY u.id, u.name, u.avatar_url
            ORDER BY blood_count DESC
            LIMIT ?
        """, (*machine_ids, limit))
        return cursor.fetchall()

    def get_stale_users(self, cutoff):
        """Users with no team_activity on or after `cutoff` (or none at all),
        ordered with never-active users first, then oldest-active next.
        """
        cursor = self._connection.execute("""
            SELECT u.id, u.name, MAX(t.date) AS last_active
            FROM users u
            LEFT JOIN team_activity t ON t.user_id = u.id
            GROUP BY u.id, u.name
            HAVING last_active IS NULL OR last_active < ?
            ORDER BY (last_active IS NULL) DESC, last_active ASC
        """, (cutoff,))
        return cursor.fetchall()

    def get_machine(self, machine_id):
        """Fetches a machine row by ID, or None if not found."""
        cursor = self._connection.execute(
            "SELECT * FROM machines WHERE id = ?", (machine_id,)
        )
        return cursor.fetchone()

    def get_machine_by_name(self, name):
        """Fetches a machine row by name (case-insensitive), or None."""
        cursor = self._connection.execute(
            "SELECT * FROM machines WHERE name = ? COLLATE NOCASE", (name,)
        )
        return cursor.fetchone()

    def get_challenge(self, challenge_id):
        """Fetches a challenge row by ID, or None if not found."""
        cursor = self._connection.execute(
            "SELECT * FROM challenges WHERE id = ?", (challenge_id,)
        )
        return cursor.fetchone()

    def get_challenge_by_name(self, name):
        """Fetches a challenge row by name (case-insensitive), or None."""
        cursor = self._connection.execute(
            "SELECT * FROM challenges WHERE name = ? COLLATE NOCASE", (name,)
        )
        return cursor.fetchone()

    def get_sherlock(self, sherlock_id):
        """Fetches a sherlock row by ID, or None if not found."""
        cursor = self._connection.execute(
            "SELECT * FROM sherlocks WHERE id = ?", (sherlock_id,)
        )
        return cursor.fetchone()

    def get_sherlock_by_name(self, name):
        """Fetches a sherlock row by name (case-insensitive), or None."""
        cursor = self._connection.execute(
            "SELECT * FROM sherlocks WHERE name = ? COLLATE NOCASE", (name,)
        )
        return cursor.fetchone()

    def get_solve_pair(self, object_type, object_id, user_id_a, user_id_b, solve_type=None):
        """Fetches both users' own_date/blood/team_blood rows (if any) for the same
        object from the authoritative holder table -- i.e. full lifetime history,
        not the team_activity rolling window. Used by !andor to compare who really
        solved something first.
        """
        if object_type == "machine":
            table, id_col = "user_machine_solves", "machine_id"
            extra_sql, extra_params = " AND solve_type = ?", (solve_type,)
        elif object_type == "challenge":
            table, id_col = "user_challenge_solves", "challenge_id"
            extra_sql, extra_params = "", ()
        elif object_type == "sherlock":
            table, id_col = "user_sherlock_solves", "sherlock_id"
            extra_sql, extra_params = "", ()
        else:
            raise ValueError(f"Unknown object_type: {object_type}")

        rows = {}
        for uid in (user_id_a, user_id_b):
            rows[uid] = self._connection.execute(
                f"SELECT own_date, blood, team_blood FROM {table} "
                f"WHERE {id_col} = ? AND user_id = ?{extra_sql}",
                (object_id, uid, *extra_params),
            ).fetchone()
        return rows

    # ── discord_users ──────────────────────────────────────────────────────────

    def get_discord_user(self, discord_id):
        """Fetches a discord_users row by Discord ID, or None if this account
        has no row yet (never claimed, never had a role set).
        """
        cursor = self._connection.execute(
            "SELECT * FROM discord_users WHERE discord_id = ?", (discord_id,)
        )
        return cursor.fetchone()

    def get_discord_role(self, discord_id):
        """Returns this Discord account's permission role, or 0 (everyone) if
        it has no row yet.
        """
        cursor = self._connection.execute(
            "SELECT role FROM discord_users WHERE discord_id = ?", (discord_id,)
        )
        row = cursor.fetchone()
        return row["role"] if row else 0

    def set_discord_role(self, discord_id, role):
        """Sets a Discord account's permission role (creating its
        discord_users row if it doesn't exist yet).
        """
        self._connection.execute("""
            INSERT INTO discord_users (discord_id, role)
            VALUES (?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET role=excluded.role
        """, (discord_id, role))
        self._connection.commit()

    def get_admins(self):
        """Every Discord account with an elevated role (admin or owner),
        highest role first.
        """
        cursor = self._connection.execute(
            "SELECT discord_id, role FROM discord_users WHERE role > 0 ORDER BY role DESC"
        )
        return cursor.fetchall()

    def link_htb_user(self, discord_id, htb_user_id):
        """Returns True on success, False if htb_user_id is already linked to a
        different discord_id (enforced by idx_discord_users_htb_user_id -- this is
        the hard backstop for the one-discord-account-per-HTB-profile rule, in case
        two approvals ever race past the application-level check in cmd_claim_approve).
        """
        try:
            self._connection.execute("""
                INSERT INTO discord_users (discord_id, htb_user_id)
                VALUES (?, ?)
                ON CONFLICT(discord_id) DO UPDATE SET htb_user_id=excluded.htb_user_id
            """, (discord_id, htb_user_id))
            self._connection.commit()
            return True
        except sqlite3.IntegrityError:
            self._connection.rollback()
            return False

    def set_avatar_pref(self, discord_id, pref):
        """Sets whether this claimed user's cards should show their HTB or
        Discord avatar ("htb" or "discord").
        """
        self._connection.execute("""
            INSERT INTO discord_users (discord_id, avatar_pref)
            VALUES (?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET avatar_pref=excluded.avatar_pref
        """, (discord_id, pref))
        self._connection.commit()

    def set_tag(self, discord_id, tag):
        """Sets the short tag shown on this claimed user's !stats card."""
        self._connection.execute("""
            INSERT INTO discord_users (discord_id, tag)
            VALUES (?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET tag=excluded.tag
        """, (discord_id, tag))
        self._connection.commit()

    def get_discord_id_for_htb_user(self, htb_user_id):
        """Returns the Discord ID linked to this HTB user, or None if
        unclaimed.
        """
        cursor = self._connection.execute(
            "SELECT discord_id FROM discord_users WHERE htb_user_id = ?", (htb_user_id,)
        )
        row = cursor.fetchone()
        return row["discord_id"] if row else None

    # ── claims ─────────────────────────────────────────────────────────────────

    def create_claim(self, discord_id, htb_user_id, requested_at):
        """Creates a pending claim request and returns its new ID."""
        cursor = self._connection.execute("""
            INSERT INTO claims (discord_id, htb_user_id, status, requested_at)
            VALUES (?, ?, 'pending', ?)
        """, (discord_id, htb_user_id, requested_at))
        self._connection.commit()
        return cursor.lastrowid

    def get_claim(self, claim_id):
        """Fetches a claim row by ID, or None if not found."""
        cursor = self._connection.execute(
            "SELECT * FROM claims WHERE id = ?", (claim_id,)
        )
        return cursor.fetchone()

    def get_pending_claim_for_discord(self, discord_id):
        """Fetches this Discord account's pending claim (with the target HTB
        username joined in), or None if it has none.
        """
        cursor = self._connection.execute("""
            SELECT c.*, u.name AS htb_name FROM claims c
            JOIN users u ON u.id = c.htb_user_id
            WHERE c.discord_id = ? AND c.status = 'pending'
        """, (discord_id,))
        return cursor.fetchone()

    def get_pending_claims(self):
        """All pending claims (with target HTB usernames joined in), oldest
        first.
        """
        cursor = self._connection.execute("""
            SELECT c.*, u.name AS htb_name FROM claims c
            JOIN users u ON u.id = c.htb_user_id
            WHERE c.status = 'pending'
            ORDER BY c.requested_at ASC
        """)
        return cursor.fetchall()

    def resolve_claim(self, claim_id, status, resolved_by, resolved_at):
        """Marks a claim as approved/denied, recording who resolved it and
        when.
        """
        self._connection.execute("""
            UPDATE claims SET status = ?, resolved_by = ?, resolved_at = ?
            WHERE id = ?
        """, (status, resolved_by, resolved_at, claim_id))
        self._connection.commit()

    def purge_user(self, user_id):
        """Permanently removes a user and every trace of their history (moderation
        action, e.g. a confirmed cheater). Any team blood they held is reassigned to
        whichever remaining teammate solved that object next, if anyone did.
        Returns a summary dict for reporting back to the admin who ran it.
        """
        conn = self._connection
        reassigned = []

        machine_bloods = conn.execute("""
            SELECT s.machine_id, s.solve_type, m.name AS machine_name
            FROM user_machine_solves s JOIN machines m ON m.id = s.machine_id
            WHERE s.user_id = ? AND s.team_blood = 1
        """, (user_id,)).fetchall()
        for row in machine_bloods:
            next_holder = conn.execute("""
                SELECT s.user_id, u.name AS user_name
                FROM user_machine_solves s JOIN users u ON u.id = s.user_id
                WHERE s.machine_id = ? AND s.solve_type = ? AND s.user_id != ?
                ORDER BY s.own_date ASC LIMIT 1
            """, (row["machine_id"], row["solve_type"], user_id)).fetchone()
            label = f"{row['machine_name']} ({row['solve_type']})"
            if next_holder:
                conn.execute("""
                    UPDATE user_machine_solves SET team_blood = 1
                    WHERE machine_id = ? AND solve_type = ? AND user_id = ?
                """, (row["machine_id"], row["solve_type"], next_holder["user_id"]))
                reassigned.append(f"{label} -> {next_holder['user_name']}")
            else:
                reassigned.append(f"{label} -> unheld (no other team solve)")

        challenge_bloods = conn.execute("""
            SELECT s.challenge_id, c.name AS challenge_name
            FROM user_challenge_solves s JOIN challenges c ON c.id = s.challenge_id
            WHERE s.user_id = ? AND s.team_blood = 1
        """, (user_id,)).fetchall()
        for row in challenge_bloods:
            next_holder = conn.execute("""
                SELECT s.user_id, u.name AS user_name
                FROM user_challenge_solves s JOIN users u ON u.id = s.user_id
                WHERE s.challenge_id = ? AND s.user_id != ?
                ORDER BY s.own_date ASC LIMIT 1
            """, (row["challenge_id"], user_id)).fetchone()
            if next_holder:
                conn.execute("""
                    UPDATE user_challenge_solves SET team_blood = 1
                    WHERE challenge_id = ? AND user_id = ?
                """, (row["challenge_id"], next_holder["user_id"]))
                reassigned.append(f"{row['challenge_name']} -> {next_holder['user_name']}")
            else:
                reassigned.append(f"{row['challenge_name']} -> unheld (no other team solve)")

        sherlock_bloods = conn.execute("""
            SELECT s.sherlock_id, sh.name AS sherlock_name
            FROM user_sherlock_solves s JOIN sherlocks sh ON sh.id = s.sherlock_id
            WHERE s.user_id = ? AND s.team_blood = 1
        """, (user_id,)).fetchall()
        for row in sherlock_bloods:
            next_holder = conn.execute("""
                SELECT s.user_id, u.name AS user_name
                FROM user_sherlock_solves s JOIN users u ON u.id = s.user_id
                WHERE s.sherlock_id = ? AND s.user_id != ?
                ORDER BY s.own_date ASC LIMIT 1
            """, (row["sherlock_id"], user_id)).fetchone()
            if next_holder:
                conn.execute("""
                    UPDATE user_sherlock_solves SET team_blood = 1
                    WHERE sherlock_id = ? AND user_id = ?
                """, (row["sherlock_id"], next_holder["user_id"]))
                reassigned.append(f"{row['sherlock_name']} -> {next_holder['user_name']}")
            else:
                reassigned.append(f"{row['sherlock_name']} -> unheld (no other team solve)")

        counts = {}
        for label, table in [
            ("machine_solves",   "user_machine_solves"),
            ("challenge_solves", "user_challenge_solves"),
            ("sherlock_solves",  "user_sherlock_solves"),
            ("prolab_rows",      "user_prolab_progress"),
            ("fortress_rows",    "user_fortress_progress"),
            ("activity_rows",    "team_activity"),
            ("claim_rows",       "claims"),
        ]:
            id_col = "htb_user_id" if table == "claims" else "user_id"
            counts[label] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {id_col} = ?", (user_id,)
            ).fetchone()[0]

        conn.execute("DELETE FROM user_machine_solves WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_challenge_solves WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_sherlock_solves WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_prolab_progress WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_fortress_progress WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM team_activity WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM claims WHERE htb_user_id = ?", (user_id,))
        conn.execute("UPDATE discord_users SET htb_user_id = NULL WHERE htb_user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()

        return {"counts": counts, "reassigned": reassigned}

    def get_all_avatar_urls(self):
        """Every distinct avatar_url stored anywhere in the DB, used by
        !trimcache to work out which cached files are still referenced.
        """
        urls = set()
        for table, col in [
            ("users",      "avatar_url"),
            ("machines",   "avatar_url"),
            ("challenges", "avatar_url"),
            ("sherlocks",  "avatar_url"),
            ("prolabs",    "avatar_url"),
            ("fortresses", "avatar_url"),
        ]:
            for row in self._connection.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL"):
                urls.add(row[0])
        return urls

    def get_table_counts(self):
        """Row count for every tracked table, for !dbstats."""
        tables = [
            "team_activity", "users", "discord_users", "claims",
            "machines", "challenges", "sherlocks", "prolabs", "fortresses",
            "user_machine_solves", "user_challenge_solves", "user_sherlock_solves",
            "user_prolab_progress", "user_fortress_progress",
        ]
        return {
            t: self._connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in tables
        }


_instance = None


def get():
    """Returns the active Database instance. Raises DatabaseError if init()
    hasn't been called yet.
    """
    if _instance is None:
        raise DatabaseError("Database has not been initialized. Call init() first.")
    return _instance


def init(filename):
    """Opens (or creates) the SQLite database at the given path and sets it
    as the active instance returned by get().
    """
    global _instance
    try:
        logger.info(f"Initializing database: {filename}")
        _instance = Database(filename)
        return _instance
    except sqlite3.Error as e:
        raise DatabaseError(f"Failed to initialize: {e}")
