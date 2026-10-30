# Spec: Performance Improvements

## MODIFIED Requirements

### Requirement: REQ-P-P0-2: Batch FTS Snippet Queries in search()

**Status**: MODIFIED

The `search()` function in `knowbase/index.py` SHALL be optimized to address two performance issues:

1. **N+1 FTS snippet queries (L448-459)**: For each result, a separate `SELECT snippet(mem_fts, ...)` query is executed against SQLite FTS. This SHALL be replaced with a single batch query joining all eligible IDs.

2. **Full meta table scan (L410-426)**: The entire `meta` table is scanned row-by-row to filter eligibility. When combined with per-row `entity_heads` and `source_current` lookups, this causes O(N) overhead. A filtered SQL query with WHERE clauses SHALL be used instead of post-filtering.

#### Scenario: Search with 50 results
- **Given** 50 eligible memory entries
- **When** `search()` is executed
- **Then** snippets for all 50 entries are retrieved in a single FTS query (e.g., using `WHERE id IN (...)`) , not 50 individual queries

#### Scenario: Search with type and scope filters
- **Given** `search(conn, query, mtype="card", scope="myproject")` is called
- **When** meta table is scanned
- **Then** the SQL query includes `WHERE type='card' AND scope IN ('myproject','global') AND status != 'archived'` to minimize Python-side filtering

#### Scenario: Search with empty result set
- **Given** no entries match the query
- **When** `search()` returns `[]`
- **Then** no FTS snippet queries are executed, and no meta rows are iterated beyond the filtered SQL result

---

### Requirement: REQ-P-P0-3: Cache load_config()

**Status**: MODIFIED

`load_config()` in `knowbase/config.py` (L107-130) SHALL cache its result to avoid repeated file I/O and TOML parsing within the same process lifetime. The cache SHALL be invalidated when the configuration file's modification time changes.

#### Scenario: Multiple calls within one process
- **Given** `load_config()` is called 10 times from different modules during a single `knowbase search`
- **When** the config file has not changed on disk
- **Then** the TOML file is read and parsed only once; subsequent calls return the cached dict

#### Scenario: Configuration file modified externally
- **Given** the config file's mtime has changed since last cache
- **When** `load_config()` is called again
- **Then** the file is re-read and parsed, cache is updated with the new content

#### Scenario: Configuration file deleted or unreadable
- **Given** the config file does not exist or permissions prevent reading
- **When** `load_config()` is called
- **Then** the function returns the default config dict (same behavior as today), and no cache entry is stored

---

### Requirement: REQ-P-P0-4: Incremental Index Rebuild

**Status**: MODIFIED

`build_index()` in `knowbase/index.py` currently calls `rebuild()` which iterates over all entities via `iter_all()`. For large knowledge bases, this is wasteful when only a few entities have changed. The rebuild path SHALL detect incremental changes and only re-index modified entities.

#### Scenario: Full rebuild on first run
- **Given** the FTS index is empty or missing
- **When** `build_index()` is called
- **Then** `rebuild()` performs a full iteration and populates all entities

#### Scenario: Incremental update after single entity change
- **Given** one entity has been updated since last index build
- **When** `build_index()` is called
- **Then** only the changed entity is re-indexed; previously indexed entities are skipped based on their content_revision

#### Scenario: Forced full rebuild
- **Given** `build_index()` is called with an explicit `force=True` parameter or the incremental detection encounters a schema change
- **When** the rebuild proceeds
- **Then** all entities are re-indexed regardless of change detection

---

### Requirement: REQ-P-P1-9: Batch Multi-Word FTS Query in _match_ids()

**Status**: MODIFIED

`_match_ids()` in `knowbase/index.py` (L247-260) currently performs separate FTS queries for each word in a multi-word query, then aggregates hit counts. This SHALL be replaced with a single FTS query combining all terms.

#### Scenario: Two-word query
- **Given** `_match_ids(conn, ["hello", "world"])` is called
- **When** the FTS query is executed
- **Then** a single query like `SELECT id, rank FROM mem_fts WHERE mem_fts MATCH 'hello OR world' ORDER BY rank` is issued instead of two separate queries

#### Scenario: Single-word query
- **Given** `_match_ids(conn, ["hello"])` is called
- **When** the FTS query is executed
- **Then** the behavior is unchanged from the current implementation (no unnecessary complexity added for the single-word case)

#### Scenario: Query with stop words or special characters
- **Given** `_match_ids(conn, ["the", "a", "hello"])` is called
- **When** terms are sanitized for FTS
- **Then** stop words and special characters are filtered before constructing the FTS query, preventing FTS syntax errors

---

### Requirement: REQ-P-P2-14: Compile Regex Patterns as Module-Level Constants

**Status**: MODIFIED

Three regex patterns in `knowbase/extraction/segmenter.py` SHALL be compiled as module-level constants instead of being recompiled on every call. Current hot-path patterns:

- L87: `re.match(r'^(#{1,6})\s+(.+)$', line)` — executed for every line during markdown parsing (heading detection)
- L130: `re.split(r'\n\s*\n', text)` — executed for each text block during paragraph splitting
- L203: `re.match(pattern, line)` — executed for each line during pattern-based extraction

These SHALL be promoted to module-level compiled constants.

#### Scenario: Markdown heading parsing
- **Given** a 1000-line markdown document
- **When** headings are detected
- **Then** `re.match(HEADING_RE, line)` uses the pre-compiled `HEADING_RE` pattern instead of recompiling 1000 times

#### Scenario: Paragraph splitting
- **Given** a large text body
- **When** paragraphs are split
- **Then** `PARAGRAPH_SPLIT_RE.split(text)` uses the pre-compiled pattern

#### Scenario: Pattern-based extraction
- **Given** a pattern matching loop iterates over extracted lines
- **When** `re.match(pattern, line)` is called
- **Then** `pattern` is already a compiled `re.Pattern` object, not recompiled per-line