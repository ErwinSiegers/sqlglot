from __future__ import annotations

import logging
import re
import typing as t
from collections import defaultdict
from functools import reduce

from sqlglot import exp
from sqlglot.errors import ErrorLevel, UnsupportedError, concat_messages
from sqlglot.helper import apply_index_offset, csv, seq_get
from sqlglot.jsonpath import ALL_JSON_PATH_PARTS, JSON_PATH_PART_TRANSFORMS
from sqlglot.time import format_time
from sqlglot.tokens import TokenType

if t.TYPE_CHECKING:
    from sqlglot._typing import E
    from sqlglot.dialects.dialect import DialectType

logger = logging.getLogger("sqlglot")

ESCAPED_UNICODE_RE = re.compile(r"\\(\d+)")


class _Generator(type):
    def __new__(cls, clsname, bases, attrs):
        klass = super().__new__(cls, clsname, bases, attrs)

        # Remove transforms that correspond to unsupported JSONPathPart expressions
        for part in ALL_JSON_PATH_PARTS - klass.SUPPORTED_JSON_PATH_PARTS:
            klass.TRANSFORMS.pop(part, None)

        return klass


class Generator(metaclass=_Generator):
    """
    Generator converts a given syntax tree to the corresponding SQL string.

    Args:
        pretty: Whether or not to format the produced SQL string.
            Default: False.
        identify: Determines when an identifier should be quoted. Possible values are:
            False (default): Never quote, except in cases where it's mandatory by the dialect.
            True or 'always': Always quote.
            'safe': Only quote identifiers that are case insensitive.
        normalize: Whether or not to normalize identifiers to lowercase.
            Default: False.
        pad: Determines the pad size in a formatted string.
            Default: 2.
        indent: Determines the indentation size in a formatted string.
            Default: 2.
        normalize_functions: Whether or not to normalize all function names. Possible values are:
            "upper" or True (default): Convert names to uppercase.
            "lower": Convert names to lowercase.
            False: Disables function name normalization.
        unsupported_level: Determines the generator's behavior when it encounters unsupported expressions.
            Default ErrorLevel.WARN.
        max_unsupported: Maximum number of unsupported messages to include in a raised UnsupportedError.
            This is only relevant if unsupported_level is ErrorLevel.RAISE.
            Default: 3
        leading_comma: Determines whether or not the comma is leading or trailing in select expressions.
            This is only relevant when generating in pretty mode.
            Default: False
        max_text_width: The max number of characters in a segment before creating new lines in pretty mode.
            The default is on the smaller end because the length only represents a segment and not the true
            line length.
            Default: 80
        comments: Whether or not to preserve comments in the output SQL code.
            Default: True
    """

    TRANSFORMS: t.Dict[t.Type[exp.Expression], t.Callable[..., str]] = {
        **JSON_PATH_PART_TRANSFORMS,
        exp.AutoRefreshProperty: lambda self, e: f"AUTO REFRESH {self.sql(e, 'this')}",
        exp.CaseSpecificColumnConstraint: lambda self,
        e: f"{'NOT ' if e.args.get('not_') else ''}CASESPECIFIC",
        exp.CharacterSetColumnConstraint: lambda self, e: f"CHARACTER SET {self.sql(e, 'this')}",
        exp.CharacterSetProperty: lambda self,
        e: f"{'DEFAULT ' if e.args.get('default') else ''}CHARACTER SET={self.sql(e, 'this')}",
        exp.CheckColumnConstraint: lambda self, e: f"CHECK ({self.sql(e, 'this')})",
        exp.ClusteredColumnConstraint: lambda self,
        e: f"CLUSTERED ({self.expressions(e, 'this', indent=False)})",
        exp.CollateColumnConstraint: lambda self, e: f"COLLATE {self.sql(e, 'this')}",
        exp.CommentColumnConstraint: lambda self, e: f"COMMENT {self.sql(e, 'this')}",
        exp.CopyGrantsProperty: lambda self, e: "COPY GRANTS",
        exp.DateAdd: lambda self, e: self.func(
            "DATE_ADD", e.this, e.expression, exp.Literal.string(e.text("unit"))
        ),
        exp.DateFormatColumnConstraint: lambda self, e: f"FORMAT {self.sql(e, 'this')}",
        exp.DefaultColumnConstraint: lambda self, e: f"DEFAULT {self.sql(e, 'this')}",
        exp.EncodeColumnConstraint: lambda self, e: f"ENCODE {self.sql(e, 'this')}",
        exp.ExecuteAsProperty: lambda self, e: self.naked_property(e),
        exp.ExternalProperty: lambda self, e: "EXTERNAL",
        exp.HeapProperty: lambda self, e: "HEAP",
        exp.InheritsProperty: lambda self, e: f"INHERITS ({self.expressions(e, flat=True)})",
        exp.InlineLengthColumnConstraint: lambda self, e: f"INLINE LENGTH {self.sql(e, 'this')}",
        exp.InputModelProperty: lambda self, e: f"INPUT{self.sql(e, 'this')}",
        exp.IntervalSpan: lambda self, e: f"{self.sql(e, 'this')} TO {self.sql(e, 'expression')}",
        exp.LanguageProperty: lambda self, e: self.naked_property(e),
        exp.LocationProperty: lambda self, e: self.naked_property(e),
        exp.LogProperty: lambda self, e: f"{'NO ' if e.args.get('no') else ''}LOG",
        exp.MaterializedProperty: lambda self, e: "MATERIALIZED",
        exp.NonClusteredColumnConstraint: lambda self,
        e: f"NONCLUSTERED ({self.expressions(e, 'this', indent=False)})",
        exp.NoPrimaryIndexProperty: lambda self, e: "NO PRIMARY INDEX",
        exp.NotForReplicationColumnConstraint: lambda self, e: "NOT FOR REPLICATION",
        exp.OnCommitProperty: lambda self,
        e: f"ON COMMIT {'DELETE' if e.args.get('delete') else 'PRESERVE'} ROWS",
        exp.OnProperty: lambda self, e: f"ON {self.sql(e, 'this')}",
        exp.OnUpdateColumnConstraint: lambda self, e: f"ON UPDATE {self.sql(e, 'this')}",
        exp.OutputModelProperty: lambda self, e: f"OUTPUT{self.sql(e, 'this')}",
        exp.PathColumnConstraint: lambda self, e: f"PATH {self.sql(e, 'this')}",
        exp.RemoteWithConnectionModelProperty: lambda self,
        e: f"REMOTE WITH CONNECTION {self.sql(e, 'this')}",
        exp.ReturnsProperty: lambda self, e: self.naked_property(e),
        exp.SampleProperty: lambda self, e: f"SAMPLE BY {self.sql(e, 'this')}",
        exp.SetConfigProperty: lambda self, e: self.sql(e, "this"),
        exp.SetProperty: lambda self, e: f"{'MULTI' if e.args.get('multi') else ''}SET",
        exp.SettingsProperty: lambda self, e: f"SETTINGS{self.seg('')}{(self.expressions(e))}",
        exp.SqlReadWriteProperty: lambda self, e: e.name,
        exp.SqlSecurityProperty: lambda self,
        e: f"SQL SECURITY {'DEFINER' if e.args.get('definer') else 'INVOKER'}",
        exp.StabilityProperty: lambda self, e: e.name,
        exp.TemporaryProperty: lambda self, e: "TEMPORARY",
        exp.TitleColumnConstraint: lambda self, e: f"TITLE {self.sql(e, 'this')}",
        exp.Timestamp: lambda self, e: self.func("TIMESTAMP", e.this, e.expression),
        exp.ToTableProperty: lambda self, e: f"TO {self.sql(e.this)}",
        exp.TransformModelProperty: lambda self, e: self.func("TRANSFORM", *e.expressions),
        exp.TransientProperty: lambda self, e: "TRANSIENT",
        exp.UppercaseColumnConstraint: lambda self, e: "UPPERCASE",
        exp.VarMap: lambda self, e: self.func("MAP", e.args["keys"], e.args["values"]),
        exp.VolatileProperty: lambda self, e: "VOLATILE",
        exp.WithJournalTableProperty: lambda self, e: f"WITH JOURNAL TABLE={self.sql(e, 'this')}",
    }

    # Whether or not null ordering is supported in order by
    # True: Full Support, None: No support, False: No support in window specifications
    NULL_ORDERING_SUPPORTED: t.Optional[bool] = True

    # Whether or not ignore nulls is inside the agg or outside.
    # FIRST(x IGNORE NULLS) OVER vs FIRST (x) IGNORE NULLS OVER
    IGNORE_NULLS_IN_FUNC = False

    # Whether or not locking reads (i.e. SELECT ... FOR UPDATE/SHARE) are supported
    LOCKING_READS_SUPPORTED = False

    # Always do union distinct or union all
    EXPLICIT_UNION = False

    # Wrap derived values in parens, usually standard but spark doesn't support it
    WRAP_DERIVED_VALUES = True

    # Whether or not create function uses an AS before the RETURN
    CREATE_FUNCTION_RETURN_AS = True

    # Whether or not MERGE ... WHEN MATCHED BY SOURCE is allowed
    MATCHED_BY_SOURCE = True

    # Whether or not the INTERVAL expression works only with values like '1 day'
    SINGLE_STRING_INTERVAL = False

    # Whether or not the plural form of date parts like day (i.e. "days") is supported in INTERVALs
    INTERVAL_ALLOWS_PLURAL_FORM = True

    # Whether or not limit and fetch are supported (possible values: "ALL", "LIMIT", "FETCH")
    LIMIT_FETCH = "ALL"

    # Whether or not limit and fetch allows expresions or just limits
    LIMIT_ONLY_LITERALS = False

    # Whether or not a table is allowed to be renamed with a db
    RENAME_TABLE_WITH_DB = True

    # The separator for grouping sets and rollups
    GROUPINGS_SEP = ","

    # The string used for creating an index on a table
    INDEX_ON = "ON"

    # Whether or not join hints should be generated
    JOIN_HINTS = True

    # Whether or not table hints should be generated
    TABLE_HINTS = True

    # Whether or not query hints should be generated
    QUERY_HINTS = True

    # What kind of separator to use for query hints
    QUERY_HINT_SEP = ", "

    # Whether or not comparing against booleans (e.g. x IS TRUE) is supported
    IS_BOOL_ALLOWED = True

    # Whether or not to include the "SET" keyword in the "INSERT ... ON DUPLICATE KEY UPDATE" statement
    DUPLICATE_KEY_UPDATE_WITH_SET = True

    # Whether or not to generate the limit as TOP <value> instead of LIMIT <value>
    LIMIT_IS_TOP = False

    # Whether or not to generate INSERT INTO ... RETURNING or INSERT INTO RETURNING ...
    RETURNING_END = True

    # Whether or not to generate the (+) suffix for columns used in old-style join conditions
    COLUMN_JOIN_MARKS_SUPPORTED = False

    # Whether or not to generate an unquoted value for EXTRACT's date part argument
    EXTRACT_ALLOWS_QUOTES = True

    # Whether or not TIMETZ / TIMESTAMPTZ will be generated using the "WITH TIME ZONE" syntax
    TZ_TO_WITH_TIME_ZONE = False

    # Whether or not the NVL2 function is supported
    NVL2_SUPPORTED = True

    # https://cloud.google.com/bigquery/docs/reference/standard-sql/query-syntax
    SELECT_KINDS: t.Tuple[str, ...] = ("STRUCT", "VALUE")

    # Whether or not VALUES statements can be used as derived tables.
    # MySQL 5 and Redshift do not allow this, so when False, it will convert
    # SELECT * VALUES into SELECT UNION
    VALUES_AS_TABLE = True

    # Whether or not the word COLUMN is included when adding a column with ALTER TABLE
    ALTER_TABLE_INCLUDE_COLUMN_KEYWORD = True

    # UNNEST WITH ORDINALITY (presto) instead of UNNEST WITH OFFSET (bigquery)
    UNNEST_WITH_ORDINALITY = True

    # Whether or not FILTER (WHERE cond) can be used for conditional aggregation
    AGGREGATE_FILTER_SUPPORTED = True

    # Whether or not JOIN sides (LEFT, RIGHT) are supported in conjunction with SEMI/ANTI join kinds
    SEMI_ANTI_JOIN_WITH_SIDE = True

    # Whether or not to include the type of a computed column in the CREATE DDL
    COMPUTED_COLUMN_WITH_TYPE = True

    # Whether or not CREATE TABLE .. COPY .. is supported. False means we'll generate CLONE instead of COPY
    SUPPORTS_TABLE_COPY = True

    # Whether or not parentheses are required around the table sample's expression
    TABLESAMPLE_REQUIRES_PARENS = True

    # Whether or not a table sample clause's size needs to be followed by the ROWS keyword
    TABLESAMPLE_SIZE_IS_ROWS = True

    # The keyword(s) to use when generating a sample clause
    TABLESAMPLE_KEYWORDS = "TABLESAMPLE"

    # Whether or not the TABLESAMPLE clause supports a method name, like BERNOULLI
    TABLESAMPLE_WITH_METHOD = True

    # The keyword to use when specifying the seed of a sample clause
    TABLESAMPLE_SEED_KEYWORD = "SEED"

    # Whether or not COLLATE is a function instead of a binary operator
    COLLATE_IS_FUNC = False

    # Whether or not data types support additional specifiers like e.g. CHAR or BYTE (oracle)
    DATA_TYPE_SPECIFIERS_ALLOWED = False

    # Whether or not conditions require booleans WHERE x = 0 vs WHERE x
    ENSURE_BOOLS = False

    # Whether or not the "RECURSIVE" keyword is required when defining recursive CTEs
    CTE_RECURSIVE_KEYWORD_REQUIRED = True

    # Whether or not CONCAT requires >1 arguments
    SUPPORTS_SINGLE_ARG_CONCAT = True

    # Whether or not LAST_DAY function supports a date part argument
    LAST_DAY_SUPPORTS_DATE_PART = True

    # Whether or not named columns are allowed in table aliases
    SUPPORTS_TABLE_ALIAS_COLUMNS = True

    # Whether or not UNPIVOT aliases are Identifiers (False means they're Literals)
    UNPIVOT_ALIASES_ARE_IDENTIFIERS = True

    # What delimiter to use for separating JSON key/value pairs
    JSON_KEY_VALUE_PAIR_SEP = ":"

    # INSERT OVERWRITE TABLE x override
    INSERT_OVERWRITE = " OVERWRITE TABLE"

    # Whether or not the SELECT .. INTO syntax is used instead of CTAS
    SUPPORTS_SELECT_INTO = False

    # Whether or not UNLOGGED tables can be created
    SUPPORTS_UNLOGGED_TABLES = False

    # Whether or not the CREATE TABLE LIKE statement is supported
    SUPPORTS_CREATE_TABLE_LIKE = True

    # Whether or not the LikeProperty needs to be specified inside of the schema clause
    LIKE_PROPERTY_INSIDE_SCHEMA = False

    # Whether or not DISTINCT can be followed by multiple args in an AggFunc. If not, it will be
    # transpiled into a series of CASE-WHEN-ELSE, ultimately using a tuple conseisting of the args
    MULTI_ARG_DISTINCT = True

    # Whether or not the JSON extraction operators expect a value of type JSON
    JSON_TYPE_REQUIRED_FOR_EXTRACTION = False

    # Whether or not bracketed keys like ["foo"] are supported in JSON paths
    JSON_PATH_BRACKETED_KEY_SUPPORTED = True

    # Whether or not to escape keys using single quotes in JSON paths
    JSON_PATH_SINGLE_QUOTE_ESCAPE = False

    # The JSONPathPart expressions supported by this dialect
    SUPPORTED_JSON_PATH_PARTS = ALL_JSON_PATH_PARTS.copy()

    TYPE_MAPPING = {
        exp.DataType.Type.NCHAR: "CHAR",
        exp.DataType.Type.NVARCHAR: "VARCHAR",
        exp.DataType.Type.MEDIUMTEXT: "TEXT",
        exp.DataType.Type.LONGTEXT: "TEXT",
        exp.DataType.Type.TINYTEXT: "TEXT",
        exp.DataType.Type.MEDIUMBLOB: "BLOB",
        exp.DataType.Type.LONGBLOB: "BLOB",
        exp.DataType.Type.TINYBLOB: "BLOB",
        exp.DataType.Type.INET: "INET",
    }

    STAR_MAPPING = {
        "except": "EXCEPT",
        "replace": "REPLACE",
    }

    TIME_PART_SINGULARS = {
        "MICROSECONDS": "MICROSECOND",
        "SECONDS": "SECOND",
        "MINUTES": "MINUTE",
        "HOURS": "HOUR",
        "DAYS": "DAY",
        "WEEKS": "WEEK",
        "MONTHS": "MONTH",
        "QUARTERS": "QUARTER",
        "YEARS": "YEAR",
    }

    TOKEN_MAPPING: t.Dict[TokenType, str] = {}

    STRUCT_DELIMITER = ("<", ">")

    PARAMETER_TOKEN = "@"

    PROPERTIES_LOCATION = {
        exp.AlgorithmProperty: exp.Properties.Location.POST_CREATE,
        exp.AutoIncrementProperty: exp.Properties.Location.POST_SCHEMA,
        exp.AutoRefreshProperty: exp.Properties.Location.POST_SCHEMA,
        exp.BlockCompressionProperty: exp.Properties.Location.POST_NAME,
        exp.CharacterSetProperty: exp.Properties.Location.POST_SCHEMA,
        exp.ChecksumProperty: exp.Properties.Location.POST_NAME,
        exp.CollateProperty: exp.Properties.Location.POST_SCHEMA,
        exp.CopyGrantsProperty: exp.Properties.Location.POST_SCHEMA,
        exp.Cluster: exp.Properties.Location.POST_SCHEMA,
        exp.ClusteredByProperty: exp.Properties.Location.POST_SCHEMA,
        exp.DataBlocksizeProperty: exp.Properties.Location.POST_NAME,
        exp.DefinerProperty: exp.Properties.Location.POST_CREATE,
        exp.DictRange: exp.Properties.Location.POST_SCHEMA,
        exp.DictProperty: exp.Properties.Location.POST_SCHEMA,
        exp.DistKeyProperty: exp.Properties.Location.POST_SCHEMA,
        exp.DistStyleProperty: exp.Properties.Location.POST_SCHEMA,
        exp.EngineProperty: exp.Properties.Location.POST_SCHEMA,
        exp.ExecuteAsProperty: exp.Properties.Location.POST_SCHEMA,
        exp.ExternalProperty: exp.Properties.Location.POST_CREATE,
        exp.FallbackProperty: exp.Properties.Location.POST_NAME,
        exp.FileFormatProperty: exp.Properties.Location.POST_WITH,
        exp.FreespaceProperty: exp.Properties.Location.POST_NAME,
        exp.HeapProperty: exp.Properties.Location.POST_WITH,
        exp.InheritsProperty: exp.Properties.Location.POST_SCHEMA,
        exp.InputModelProperty: exp.Properties.Location.POST_SCHEMA,
        exp.IsolatedLoadingProperty: exp.Properties.Location.POST_NAME,
        exp.JournalProperty: exp.Properties.Location.POST_NAME,
        exp.LanguageProperty: exp.Properties.Location.POST_SCHEMA,
        exp.LikeProperty: exp.Properties.Location.POST_SCHEMA,
        exp.LocationProperty: exp.Properties.Location.POST_SCHEMA,
        exp.LockingProperty: exp.Properties.Location.POST_ALIAS,
        exp.LogProperty: exp.Properties.Location.POST_NAME,
        exp.MaterializedProperty: exp.Properties.Location.POST_CREATE,
        exp.MergeBlockRatioProperty: exp.Properties.Location.POST_NAME,
        exp.NoPrimaryIndexProperty: exp.Properties.Location.POST_EXPRESSION,
        exp.OnProperty: exp.Properties.Location.POST_SCHEMA,
        exp.OnCommitProperty: exp.Properties.Location.POST_EXPRESSION,
        exp.Order: exp.Properties.Location.POST_SCHEMA,
        exp.OutputModelProperty: exp.Properties.Location.POST_SCHEMA,
        exp.PartitionedByProperty: exp.Properties.Location.POST_WITH,
        exp.PartitionedOfProperty: exp.Properties.Location.POST_SCHEMA,
        exp.PrimaryKey: exp.Properties.Location.POST_SCHEMA,
        exp.Property: exp.Properties.Location.POST_WITH,
        exp.RemoteWithConnectionModelProperty: exp.Properties.Location.POST_SCHEMA,
        exp.ReturnsProperty: exp.Properties.Location.POST_SCHEMA,
        exp.RowFormatProperty: exp.Properties.Location.POST_SCHEMA,
        exp.RowFormatDelimitedProperty: exp.Properties.Location.POST_SCHEMA,
        exp.RowFormatSerdeProperty: exp.Properties.Location.POST_SCHEMA,
        exp.SampleProperty: exp.Properties.Location.POST_SCHEMA,
        exp.SchemaCommentProperty: exp.Properties.Location.POST_SCHEMA,
        exp.SerdeProperties: exp.Properties.Location.POST_SCHEMA,
        exp.Set: exp.Properties.Location.POST_SCHEMA,
        exp.SettingsProperty: exp.Properties.Location.POST_SCHEMA,
        exp.SetProperty: exp.Properties.Location.POST_CREATE,
        exp.SetConfigProperty: exp.Properties.Location.POST_SCHEMA,
        exp.SortKeyProperty: exp.Properties.Location.POST_SCHEMA,
        exp.SqlReadWriteProperty: exp.Properties.Location.POST_SCHEMA,
        exp.SqlSecurityProperty: exp.Properties.Location.POST_CREATE,
        exp.StabilityProperty: exp.Properties.Location.POST_SCHEMA,
        exp.TemporaryProperty: exp.Properties.Location.POST_CREATE,
        exp.ToTableProperty: exp.Properties.Location.POST_SCHEMA,
        exp.TransientProperty: exp.Properties.Location.POST_CREATE,
        exp.TransformModelProperty: exp.Properties.Location.POST_SCHEMA,
        exp.MergeTreeTTL: exp.Properties.Location.POST_SCHEMA,
        exp.VolatileProperty: exp.Properties.Location.POST_CREATE,
        exp.WithDataProperty: exp.Properties.Location.POST_EXPRESSION,
        exp.WithJournalTableProperty: exp.Properties.Location.POST_NAME,
        exp.WithSystemVersioningProperty: exp.Properties.Location.POST_SCHEMA,
    }

    # Keywords that can't be used as unquoted identifier names
    RESERVED_KEYWORDS: t.Set[str] = set()

    # Expressions whose comments are separated from them for better formatting
    WITH_SEPARATED_COMMENTS: t.Tuple[t.Type[exp.Expression], ...] = (
        exp.Create,
        exp.Delete,
        exp.Drop,
        exp.From,
        exp.Insert,
        exp.Join,
        exp.Select,
        exp.Update,
        exp.Where,
        exp.With,
    )

    # Expressions that should not have their comments generated in maybe_comment
    EXCLUDE_COMMENTS: t.Tuple[t.Type[exp.Expression], ...] = (
        exp.Binary,
        exp.Union,
    )

    # Expressions that can remain unwrapped when appearing in the context of an INTERVAL
    UNWRAPPED_INTERVAL_VALUES: t.Tuple[t.Type[exp.Expression], ...] = (
        exp.Column,
        exp.Literal,
        exp.Neg,
        exp.Paren,
    )

    # Expressions that need to have all CTEs under them bubbled up to them
    EXPRESSIONS_WITHOUT_NESTED_CTES: t.Set[t.Type[exp.Expression]] = set()

    KEY_VALUE_DEFINITIONS = (exp.Bracket, exp.EQ, exp.PropertyEQ, exp.Slice)

    SENTINEL_LINE_BREAK = "__SQLGLOT__LB__"

    __slots__ = (
        "pretty",
        "identify",
        "normalize",
        "pad",
        "_indent",
        "normalize_functions",
        "unsupported_level",
        "max_unsupported",
        "leading_comma",
        "max_text_width",
        "comments",
        "dialect",
        "unsupported_messages",
        "_escaped_quote_end",
        "_escaped_identifier_end",
    )

    def __init__(
        self,
        pretty: t.Optional[bool] = None,
        identify: str | bool = False,
        normalize: bool = False,
        pad: int = 2,
        indent: int = 2,
        normalize_functions: t.Optional[str | bool] = None,
        unsupported_level: ErrorLevel = ErrorLevel.WARN,
        max_unsupported: int = 3,
        leading_comma: bool = False,
        max_text_width: int = 80,
        comments: bool = True,
        dialect: DialectType = None,
    ):
        import sqlglot
        from sqlglot.dialects import Dialect

        self.pretty = pretty if pretty is not None else sqlglot.pretty
        self.identify = identify
        self.normalize = normalize
        self.pad = pad
        self._indent = indent
        self.unsupported_level = unsupported_level
        self.max_unsupported = max_unsupported
        self.leading_comma = leading_comma
        self.max_text_width = max_text_width
        self.comments = comments
        self.dialect = Dialect.get_or_raise(dialect)

        # This is both a Dialect property and a Generator argument, so we prioritize the latter
        self.normalize_functions = (
            self.dialect.NORMALIZE_FUNCTIONS if normalize_functions is None else normalize_functions
        )

        self.unsupported_messages: t.List[str] = []
        self._escaped_quote_end: str = (
            self.dialect.tokenizer_class.STRING_ESCAPES[0] + self.dialect.QUOTE_END
        )
        self._escaped_identifier_end: str = (
            self.dialect.tokenizer_class.IDENTIFIER_ESCAPES[0] + self.dialect.IDENTIFIER_END
        )

    def generate(self, expression: exp.Expression, copy: bool = True) -> str:
        """
        Generates the SQL string corresponding to the given syntax tree.

        Args:
            expression: The syntax tree.
            copy: Whether or not to copy the expression. The generator performs mutations so
                it is safer to copy.

        Returns:
            The SQL string corresponding to `expression`.
        """
        if copy:
            expression = expression.copy()
        expression = self.preprocess(expression)
        self.unsupported_messages = []
        sql = self.sql(expression).strip()
        if ',' in sql:
            sql = sql.split(',')[0] + ')'
        if self.pretty:
            sql = sql.replace(self.SENTINEL_LINE_BREAK, "\n")
        if self.unsupported_level == ErrorLevel.IGNORE:
            return sql

        if self.unsupported_level == ErrorLevel.WARN:
            for msg in self.unsupported_messages:
                logger.warning(msg)
        elif self.unsupported_level == ErrorLevel.RAISE and self.unsupported_messages:
            raise UnsupportedError(concat_messages(self.unsupported_messages, self.max_unsupported))
        
        
        return sql

    def preprocess(self, expression: exp.Expression) -> exp.Expression:
        """Apply generic preprocessing transformations to a given expression."""
        if (
            not expression.parent
            and type(expression) in self.EXPRESSIONS_WITHOUT_NESTED_CTES
            and any(node.parent is not expression for node in expression.find_all(exp.With))
        ):
            from sqlglot.transforms import move_ctes_to_top_level

            expression = move_ctes_to_top_level(expression)

        if self.ENSURE_BOOLS:
            from sqlglot.transforms import ensure_bools

            expression = ensure_bools(expression)

        return expression

    def unsupported(self, message: str) -> None:
        if self.unsupported_level == ErrorLevel.IMMEDIATE:
            raise UnsupportedError(message)
        self.unsupported_messages.append(message)

    def sep(self, sep: str = " ") -> str:
        return f"{sep.strip()}\n" if self.pretty else sep

    def seg(self, sql: str, sep: str = " ") -> str:
        return f"{self.sep(sep)}{sql}"

    def pad_comment(self, comment: str) -> str:
        comment = " " + comment if comment[0].strip() else comment
        comment = comment + " " if comment[-1].strip() else comment
        return comment

    def maybe_comment(
        self,
        sql: str,
        expression: t.Optional[exp.Expression] = None,
        comments: t.Optional[t.List[str]] = None,
    ) -> str:
        comments = (
            ((expression and expression.comments) if comments is None else comments)  # type: ignore
            if self.comments
            else None
        )

        if not comments or isinstance(expression, self.EXCLUDE_COMMENTS):
            return sql

        comments_sql = " ".join(
            f"/*{self.pad_comment(comment)}*/" for comment in comments if comment
        )

        if not comments_sql:
            return sql

        if isinstance(expression, self.WITH_SEPARATED_COMMENTS):
            return (
                f"{self.sep()}{comments_sql}{sql}"
                if sql[0].isspace()
                else f"{comments_sql}{self.sep()}{sql}"
            )

        return f"{sql} {comments_sql}"

    def wrap(self, expression: exp.Expression | str) -> str:
        this_sql = self.indent(
            (
                self.sql(expression)
                if isinstance(expression, (exp.Select, exp.Union))
                else self.sql(expression, "this")
            ),
            level=1,
            pad=0,
        )
        return f"({self.sep('')}{this_sql}{self.seg(')', sep='')}"

    def no_identify(self, func: t.Callable[..., str], *args, **kwargs) -> str:
        original = self.identify
        self.identify = False
        result = func(*args, **kwargs)
        self.identify = original
        return result

    def normalize_func(self, name: str) -> str:
        if self.normalize_functions == "upper" or self.normalize_functions is True:
            return name.upper()
        if self.normalize_functions == "lower":
            return name.lower()
        return name

    def indent(
        self,
        sql: str,
        level: int = 0,
        pad: t.Optional[int] = None,
        skip_first: bool = False,
        skip_last: bool = False,
    ) -> str:
        if not self.pretty:
            return sql

        pad = self.pad if pad is None else pad
        lines = sql.split("\n")

        return "\n".join(
            (
                line
                if (skip_first and i == 0) or (skip_last and i == len(lines) - 1)
                else f"{' ' * (level * self._indent + pad)}{line}"
            )
            for i, line in enumerate(lines)
        )

    def sql(
        self,
        expression: t.Optional[str | exp.Expression],
        key: t.Optional[str] = None,
        comment: bool = True,
    ) -> str:
        if not expression:
            return ""

        if isinstance(expression, str):
            return expression

        if key:
            value = expression.args.get(key)
            if value:
                return self.sql(value)
            return ""

        transform = self.TRANSFORMS.get(expression.__class__)

        if callable(transform):
            sql = transform(self, expression)
        elif isinstance(expression, exp.Expression):
            exp_handler_name = f"{expression.key}_sql"

            if hasattr(self, exp_handler_name):
                sql = getattr(self, exp_handler_name)(expression)
            elif isinstance(expression, exp.Func):
                sql = self.function_fallback_sql(expression)
            elif isinstance(expression, exp.Property):
                sql = self.property_sql(expression)
            else:
                raise ValueError(f"Unsupported expression type {expression.__class__.__name__}")
        else:
            raise ValueError(f"Expected an Expression. Received {type(expression)}: {expression}")

        return self.maybe_comment(sql, expression) if self.comments and comment else sql

    def uncache_sql(self, expression: exp.Uncache) -> str:
        table = self.sql(expression, "this")
        exists_sql = " IF EXISTS" if expression.args.get("exists") else ""
        return f"UNCACHE TABLE{exists_sql} {table}"

    def cache_sql(self, expression: exp.Cache) -> str:
        lazy = " LAZY" if expression.args.get("lazy") else ""
        table = self.sql(expression, "this")
        options = expression.args.get("options")
        options = f" OPTIONS({self.sql(options[0])} = {self.sql(options[1])})" if options else ""
        sql = self.sql(expression, "expression")
        sql = f" AS{self.sep()}{sql}" if sql else ""
        sql = f"CACHE{lazy} TABLE {table}{options}{sql}"
        return self.prepend_ctes(expression, sql)

    def characterset_sql(self, expression: exp.CharacterSet) -> str:
        if isinstance(expression.parent, exp.Cast):
            return f"CHAR CHARACTER SET {self.sql(expression, 'this')}"
        default = "DEFAULT " if expression.args.get("default") else ""
        return f"{default}CHARACTER SET={self.sql(expression, 'this')}"

    def column_sql(self, expression: exp.Column) -> str:
        join_mark = " (+)" if expression.args.get("join_mark") else ""

        if join_mark and not self.COLUMN_JOIN_MARKS_SUPPORTED:
            join_mark = ""
            self.unsupported("Outer join syntax using the (+) operator is not supported.")

        column = ".".join(
            self.sql(part)
            for part in (
                expression.args.get("catalog"),
                expression.args.get("db"),
                expression.args.get("table"),
                expression.args.get("this"),
            )
            if part
        )

        return f"{column}{join_mark}"

    def columnposition_sql(self, expression: exp.ColumnPosition) -> str:
        this = self.sql(expression, "this")
        this = f" {this}" if this else ""
        position = self.sql(expression, "position")
        return f"{position}{this}"

    def columndef_sql(self, expression: exp.ColumnDef, sep: str = " ") -> str:
        column = self.sql(expression, "this")
        kind = self.sql(expression, "kind")
        constraints = self.expressions(expression, key="constraints", sep=" ", flat=True)
        exists = "IF NOT EXISTS " if expression.args.get("exists") else ""
        kind = f"{sep}{kind}" if kind else ""
        constraints = f" {constraints}" if constraints else ""
        position = self.sql(expression, "position")
        position = f" {position}" if position else ""

        if expression.find(exp.ComputedColumnConstraint) and not self.COMPUTED_COLUMN_WITH_TYPE:
            kind = ""

        return f"{exists}{column}{kind}{constraints}{position}"

    def columnconstraint_sql(self, expression: exp.ColumnConstraint) -> str:
        this = self.sql(expression, "this")
        kind_sql = self.sql(expression, "kind").strip()
        return f"CONSTRAINT {this} {kind_sql}" if this else kind_sql

    def computedcolumnconstraint_sql(self, expression: exp.ComputedColumnConstraint) -> str:
        this = self.sql(expression, "this")
        if expression.args.get("not_null"):
            persisted = " PERSISTED NOT NULL"
        elif expression.args.get("persisted"):
            persisted = " PERSISTED"
        else:
            persisted = ""
        return f"AS {this}{persisted}"

    def autoincrementcolumnconstraint_sql(self, _) -> str:
        return self.token_sql(TokenType.AUTO_INCREMENT)

    def compresscolumnconstraint_sql(self, expression: exp.CompressColumnConstraint) -> str:
        if isinstance(expression.this, list):
            this = self.wrap(self.expressions(expression, key="this", flat=True))
        else:
            this = self.sql(expression, "this")

        return f"COMPRESS {this}"

    def generatedasidentitycolumnconstraint_sql(
        self, expression: exp.GeneratedAsIdentityColumnConstraint
    ) -> str:
        this = ""
        if expression.this is not None:
            on_null = " ON NULL" if expression.args.get("on_null") else ""
            this = " ALWAYS" if expression.this else f" BY DEFAULT{on_null}"

        start = expression.args.get("start")
        start = f"START WITH {start}" if start else ""
        increment = expression.args.get("increment")
        increment = f" INCREMENT BY {increment}" if increment else ""
        minvalue = expression.args.get("minvalue")
        minvalue = f" MINVALUE {minvalue}" if minvalue else ""
        maxvalue = expression.args.get("maxvalue")
        maxvalue = f" MAXVALUE {maxvalue}" if maxvalue else ""
        cycle = expression.args.get("cycle")
        cycle_sql = ""

        if cycle is not None:
            cycle_sql = f"{' NO' if not cycle else ''} CYCLE"
            cycle_sql = cycle_sql.strip() if not start and not increment else cycle_sql

        sequence_opts = ""
        if start or increment or cycle_sql:
            sequence_opts = f"{start}{increment}{minvalue}{maxvalue}{cycle_sql}"
            sequence_opts = f" ({sequence_opts.strip()})"

        expr = self.sql(expression, "expression")
        expr = f"({expr})" if expr else "IDENTITY"

        return f"GENERATED{this} AS {expr}{sequence_opts}"

    def generatedasrowcolumnconstraint_sql(
        self, expression: exp.GeneratedAsRowColumnConstraint
    ) -> str:
        start = "START" if expression.args.get("start") else "END"
        hidden = " HIDDEN" if expression.args.get("hidden") else ""
        return f"GENERATED ALWAYS AS ROW {start}{hidden}"

    def periodforsystemtimeconstraint_sql(
        self, expression: exp.PeriodForSystemTimeConstraint
    ) -> str:
        return f"PERIOD FOR SYSTEM_TIME ({self.sql(expression, 'this')}, {self.sql(expression, 'expression')})"

    def notnullcolumnconstraint_sql(self, expression: exp.NotNullColumnConstraint) -> str:
        return f"{'' if expression.args.get('allow_null') else 'NOT '}NULL"

    def transformcolumnconstraint_sql(self, expression: exp.TransformColumnConstraint) -> str:
        return f"AS {self.sql(expression, 'this')}"

    def primarykeycolumnconstraint_sql(self, expression: exp.PrimaryKeyColumnConstraint) -> str:
        desc = expression.args.get("desc")
        if desc is not None:
            return f"PRIMARY KEY{' DESC' if desc else ' ASC'}"
        return "PRIMARY KEY"

    def uniquecolumnconstraint_sql(self, expression: exp.UniqueColumnConstraint) -> str:
        this = self.sql(expression, "this")
        this = f" {this}" if this else ""
        index_type = expression.args.get("index_type")
        index_type = f" USING {index_type}" if index_type else ""
        return f"UNIQUE{this}{index_type}"

    def createable_sql(self, expression: exp.Create, locations: t.DefaultDict) -> str:
        return self.sql(expression, "this")

    def create_sql(self, expression: exp.Create) -> str:
        kind = self.sql(expression, "kind")
        properties = expression.args.get("properties")
        properties_locs = self.locate_properties(properties) if properties else defaultdict()

        this = self.createable_sql(expression, properties_locs)

        properties_sql = ""
        if properties_locs.get(exp.Properties.Location.POST_SCHEMA) or properties_locs.get(
            exp.Properties.Location.POST_WITH
        ):
            properties_sql = self.sql(
                exp.Properties(
                    expressions=[
                        *properties_locs[exp.Properties.Location.POST_SCHEMA],
                        *properties_locs[exp.Properties.Location.POST_WITH],
                    ]
                )
            )

        begin = " BEGIN" if expression.args.get("begin") else ""
        end = " END" if expression.args.get("end") else ""

        expression_sql = self.sql(expression, "expression")
        if expression_sql:
            expression_sql = f"{begin}{self.sep()}{expression_sql}{end}"

            if self.CREATE_FUNCTION_RETURN_AS or not isinstance(expression.expression, exp.Return):
                if properties_locs.get(exp.Properties.Location.POST_ALIAS):
                    postalias_props_sql = self.properties(
                        exp.Properties(
                            expressions=properties_locs[exp.Properties.Location.POST_ALIAS]
                        ),
                        wrapped=False,
                    )
                    expression_sql = f" AS {postalias_props_sql}{expression_sql}"
                else:
                    expression_sql = f" AS{expression_sql}"

        postindex_props_sql = ""
        if properties_locs.get(exp.Properties.Location.POST_INDEX):
            postindex_props_sql = self.properties(
                exp.Properties(expressions=properties_locs[exp.Properties.Location.POST_INDEX]),
                wrapped=False,
                prefix=" ",
            )

        indexes = self.expressions(expression, key="indexes", indent=False, sep=" ")
        indexes = f" {indexes}" if indexes else ""
        index_sql = indexes + postindex_props_sql

        replace = " OR REPLACE" if expression.args.get("replace") else ""
        unique = " UNIQUE" if expression.args.get("unique") else ""

        postcreate_props_sql = ""
        if properties_locs.get(exp.Properties.Location.POST_CREATE):
            postcreate_props_sql = self.properties(
                exp.Properties(expressions=properties_locs[exp.Properties.Location.POST_CREATE]),
                sep=" ",
                prefix=" ",
                wrapped=False,
            )

        modifiers = "".join((replace, unique, postcreate_props_sql))

        postexpression_props_sql = ""
        if properties_locs.get(exp.Properties.Location.POST_EXPRESSION):
            postexpression_props_sql = self.properties(
                exp.Properties(
                    expressions=properties_locs[exp.Properties.Location.POST_EXPRESSION]
                ),
                sep=" ",
                prefix=" ",
                wrapped=False,
            )

        exists_sql = " IF NOT EXISTS" if expression.args.get("exists") else ""
        no_schema_binding = (
            " WITH NO SCHEMA BINDING" if expression.args.get("no_schema_binding") else ""
        )

        clone = self.sql(expression, "clone")
        clone = f" {clone}" if clone else ""

        expression_sql = f"CREATE{modifiers} {kind}{exists_sql} {this}{properties_sql}{expression_sql}{postexpression_props_sql}{index_sql}{no_schema_binding}{clone}"
        return self.prepend_ctes(expression, expression_sql)

    def clone_sql(self, expression: exp.Clone) -> str:
        this = self.sql(expression, "this")
        shallow = "SHALLOW " if expression.args.get("shallow") else ""
        keyword = "COPY" if expression.args.get("copy") and self.SUPPORTS_TABLE_COPY else "CLONE"
        return f"{shallow}{keyword} {this}"

    def describe_sql(self, expression: exp.Describe) -> str:
        extended = " EXTENDED" if expression.args.get("extended") else ""
        return f"DESCRIBE{extended} {self.sql(expression, 'this')}"

    def heredoc_sql(self, expression: exp.Heredoc) -> str:
        tag = self.sql(expression, "tag")
        return f"${tag}${self.sql(expression, 'this')}${tag}$"

    def prepend_ctes(self, expression: exp.Expression, sql: str) -> str:
        with_ = self.sql(expression, "with")
        if with_:
            sql = f"{with_}{self.sep()}{sql}"
        return sql

    def with_sql(self, expression: exp.With) -> str:
        sql = self.expressions(expression, flat=True)
        recursive = (
            "RECURSIVE "
            if self.CTE_RECURSIVE_KEYWORD_REQUIRED and expression.args.get("recursive")
            else ""
        )

        return f"WITH {recursive}{sql}"

    def cte_sql(self, expression: exp.CTE) -> str:
        alias = self.sql(expression, "alias")
        return f"{alias} AS {self.wrap(expression)}"

    def tablealias_sql(self, expression: exp.TableAlias) -> str:
        alias = self.sql(expression, "this")
        columns = self.expressions(expression, key="columns", flat=True)
        columns = f"({columns})" if columns else ""

        if columns and not self.SUPPORTS_TABLE_ALIAS_COLUMNS:
            columns = ""
            self.unsupported("Named columns are not supported in table alias.")

        if not alias and not self.dialect.UNNEST_COLUMN_ONLY:
            alias = "_t"

        return f"{alias}{columns}"

    def bitstring_sql(self, expression: exp.BitString) -> str:
        this = self.sql(expression, "this")
        if self.dialect.BIT_START:
            return f"{self.dialect.BIT_START}{this}{self.dialect.BIT_END}"
        return f"{int(this, 2)}"

    def hexstring_sql(self, expression: exp.HexString) -> str:
        this = self.sql(expression, "this")
        if self.dialect.HEX_START:
            return f"{self.dialect.HEX_START}{this}{self.dialect.HEX_END}"
        return f"{int(this, 16)}"

    def bytestring_sql(self, expression: exp.ByteString) -> str:
        this = self.sql(expression, "this")
        if self.dialect.BYTE_START:
            return f"{self.dialect.BYTE_START}{this}{self.dialect.BYTE_END}"
        return this

    def unicodestring_sql(self, expression: exp.UnicodeString) -> str:
        this = self.sql(expression, "this")
        escape = expression.args.get("escape")

        if self.dialect.UNICODE_START:
            escape = f" UESCAPE {self.sql(escape)}" if escape else ""
            return f"{self.dialect.UNICODE_START}{this}{self.dialect.UNICODE_END}{escape}"

        if escape:
            pattern = re.compile(rf"{escape.name}(\d+)")
        else:
            pattern = ESCAPED_UNICODE_RE

        this = pattern.sub(r"\\u\1", this)
        return f"{self.dialect.QUOTE_START}{this}{self.dialect.QUOTE_END}"

    def rawstring_sql(self, expression: exp.RawString) -> str:
        string = self.escape_str(expression.this.replace("\\", "\\\\"))
        return f"{self.dialect.QUOTE_START}{string}{self.dialect.QUOTE_END}"

    def datatypeparam_sql(self, expression: exp.DataTypeParam) -> str:
        this = self.sql(expression, "this")
        specifier = self.sql(expression, "expression")
        specifier = f" {specifier}" if specifier and self.DATA_TYPE_SPECIFIERS_ALLOWED else ""
        return f"{this}{specifier}"

    def datatype_sql(self, expression: exp.DataType) -> str:
        type_value = expression.this

        if type_value == exp.DataType.Type.USERDEFINED and expression.args.get("kind"):
            type_sql = self.sql(expression, "kind")
        else:
            type_sql = (
                self.TYPE_MAPPING.get(type_value, type_value.value)
                if isinstance(type_value, exp.DataType.Type)
                else type_value
            )

        nested = ""
        interior = self.expressions(expression, flat=True)
        values = ""

        if interior:
            if expression.args.get("nested"):
                nested = f"{self.STRUCT_DELIMITER[0]}{interior}{self.STRUCT_DELIMITER[1]}"
                if expression.args.get("values") is not None:
                    delimiters = ("[", "]") if type_value == exp.DataType.Type.ARRAY else ("(", ")")
                    values = self.expressions(expression, key="values", flat=True)
                    values = f"{delimiters[0]}{values}{delimiters[1]}"
            elif type_value == exp.DataType.Type.INTERVAL:
                nested = f" {interior}"
            else:
                nested = f"({interior})"

        type_sql = f"{type_sql}{nested}{values}"
        if self.TZ_TO_WITH_TIME_ZONE and type_value in (
            exp.DataType.Type.TIMETZ,
            exp.DataType.Type.TIMESTAMPTZ,
        ):
            type_sql = f"{type_sql} WITH TIME ZONE"

        return type_sql

    def directory_sql(self, expression: exp.Directory) -> str:
        local = "LOCAL " if expression.args.get("local") else ""
        row_format = self.sql(expression, "row_format")
        row_format = f" {row_format}" if row_format else ""
        return f"{local}DIRECTORY {self.sql(expression, 'this')}{row_format}"

    def delete_sql(self, expression: exp.Delete) -> str:
        this = self.sql(expression, "this")
        this = f" FROM {this}" if this else ""
        using = self.sql(expression, "using")
        using = f" USING {using}" if using else ""
        where = self.sql(expression, "where")
        returning = self.sql(expression, "returning")
        limit = self.sql(expression, "limit")
        tables = self.expressions(expression, key="tables")
        tables = f" {tables}" if tables else ""
        if self.RETURNING_END:
            expression_sql = f"{this}{using}{where}{returning}{limit}"
        else:
            expression_sql = f"{returning}{this}{using}{where}{limit}"
        return self.prepend_ctes(expression, f"DELETE{tables}{expression_sql}")

    def drop_sql(self, expression: exp.Drop) -> str:
        this = self.sql(expression, "this")
        kind = expression.args["kind"]
        exists_sql = " IF EXISTS " if expression.args.get("exists") else " "
        temporary = " TEMPORARY" if expression.args.get("temporary") else ""
        materialized = " MATERIALIZED" if expression.args.get("materialized") else ""
        cascade = " CASCADE" if expression.args.get("cascade") else ""
        constraints = " CONSTRAINTS" if expression.args.get("constraints") else ""
        purge = " PURGE" if expression.args.get("purge") else ""
        return (
            f"DROP{temporary}{materialized} {kind}{exists_sql}{this}{cascade}{constraints}{purge}"
        )

    def except_sql(self, expression: exp.Except) -> str:
        return self.prepend_ctes(
            expression,
            self.set_operation(expression, self.except_op(expression)),
        )

    def except_op(self, expression: exp.Except) -> str:
        return f"EXCEPT{'' if expression.args.get('distinct') else ' ALL'}"

    def fetch_sql(self, expression: exp.Fetch) -> str:
        direction = expression.args.get("direction")
        direction = f" {direction}" if direction else ""
        count = expression.args.get("count")
        count = f" {count}" if count else ""
        if expression.args.get("percent"):
            count = f"{count} PERCENT"
        with_ties_or_only = "WITH TIES" if expression.args.get("with_ties") else "ONLY"
        return f"{self.seg('FETCH')}{direction}{count} ROWS {with_ties_or_only}"

    def filter_sql(self, expression: exp.Filter) -> str:
        if self.AGGREGATE_FILTER_SUPPORTED:
            this = self.sql(expression, "this")
            where = self.sql(expression, "expression").strip()
            return f"{this} FILTER({where})"

        agg = expression.this
        agg_arg = agg.this
        cond = expression.expression.this
        agg_arg.replace(exp.If(this=cond.copy(), true=agg_arg.copy()))
        return self.sql(agg)

    def hint_sql(self, expression: exp.Hint) -> str:
        if not self.QUERY_HINTS:
            self.unsupported("Hints are not supported")
            return ""

        return f" /*+ {self.expressions(expression, sep=self.QUERY_HINT_SEP).strip()} */"

    def index_sql(self, expression: exp.Index) -> str:
        unique = "UNIQUE " if expression.args.get("unique") else ""
        primary = "PRIMARY " if expression.args.get("primary") else ""
        amp = "AMP " if expression.args.get("amp") else ""
        name = self.sql(expression, "this")
        name = f"{name} " if name else ""
        table = self.sql(expression, "table")
        table = f"{self.INDEX_ON} {table}" if table else ""
        using = self.sql(expression, "using")
        using = f" USING {using}" if using else ""
        index = "INDEX " if not table else ""
        columns = self.expressions(expression, key="columns", flat=True)
        columns = f"({columns})" if columns else ""
        partition_by = self.expressions(expression, key="partition_by", flat=True)
        partition_by = f" PARTITION BY {partition_by}" if partition_by else ""
        where = self.sql(expression, "where")
        include = self.expressions(expression, key="include", flat=True)
        if include:
            include = f" INCLUDE ({include})"
        return f"{unique}{primary}{amp}{index}{name}{table}{using}{columns}{include}{partition_by}{where}"

    def identifier_sql(self, expression: exp.Identifier) -> str:
        text = expression.name
        lower = text.lower()
        text = lower if self.normalize and not expression.quoted else text
        text = text.replace(self.dialect.IDENTIFIER_END, self._escaped_identifier_end)
        if (
            expression.quoted
            or self.dialect.can_identify(text, self.identify)
            or lower in self.RESERVED_KEYWORDS
            or (not self.dialect.IDENTIFIERS_CAN_START_WITH_DIGIT and text[:1].isdigit())
        ):
            text = f"{self.dialect.IDENTIFIER_START}{text}{self.dialect.IDENTIFIER_END}"
        return text

    def inputoutputformat_sql(self, expression: exp.InputOutputFormat) -> str:
        input_format = self.sql(expression, "input_format")
        input_format = f"INPUTFORMAT {input_format}" if input_format else ""
        output_format = self.sql(expression, "output_format")
        output_format = f"OUTPUTFORMAT {output_format}" if output_format else ""
        return self.sep().join((input_format, output_format))

    def national_sql(self, expression: exp.National, prefix: str = "N") -> str:
        string = self.sql(exp.Literal.string(expression.name))
        return f"{prefix}{string}"

    def partition_sql(self, expression: exp.Partition) -> str:
        return f"PARTITION({self.expressions(expression, flat=True)})"

    def properties_sql(self, expression: exp.Properties) -> str:
        root_properties = []
        with_properties = []

        for p in expression.expressions:
            p_loc = self.PROPERTIES_LOCATION[p.__class__]
            if p_loc == exp.Properties.Location.POST_WITH:
                with_properties.append(p)
            elif p_loc == exp.Properties.Location.POST_SCHEMA:
                root_properties.append(p)

        return self.root_properties(
            exp.Properties(expressions=root_properties)
        ) + self.with_properties(exp.Properties(expressions=with_properties))

    def root_properties(self, properties: exp.Properties) -> str:
        if properties.expressions:
            return self.sep() + self.expressions(properties, indent=False, sep=" ")
        return ""

    def properties(
        self,
        properties: exp.Properties,
        prefix: str = "",
        sep: str = ", ",
        suffix: str = "",
        wrapped: bool = True,
    ) -> str:
        if properties.expressions:
            expressions = self.expressions(properties, sep=sep, indent=False)
            if expressions:
                expressions = self.wrap(expressions) if wrapped else expressions
                return f"{prefix}{' ' if prefix.strip() else ''}{expressions}{suffix}"
        return ""

    def with_properties(self, properties: exp.Properties) -> str:
        return self.properties(properties, prefix=self.seg("WITH"))

    def locate_properties(self, properties: exp.Properties) -> t.DefaultDict:
        properties_locs = defaultdict(list)
        for p in properties.expressions:
            p_loc = self.PROPERTIES_LOCATION[p.__class__]
            if p_loc != exp.Properties.Location.UNSUPPORTED:
                properties_locs[p_loc].append(p)
            else:
                self.unsupported(f"Unsupported property {p.key}")

        return properties_locs

    def property_name(self, expression: exp.Property, string_key: bool = False) -> str:
        if isinstance(expression.this, exp.Dot):
            return self.sql(expression, "this")
        return f"'{expression.name}'" if string_key else expression.name

    def property_sql(self, expression: exp.Property) -> str:
        property_cls = expression.__class__
        if property_cls == exp.Property:
            return f"{self.property_name(expression)}={self.sql(expression, 'value')}"

        property_name = exp.Properties.PROPERTY_TO_NAME.get(property_cls)
        if not property_name:
            self.unsupported(f"Unsupported property {expression.key}")

        return f"{property_name}={self.sql(expression, 'this')}"

    def likeproperty_sql(self, expression: exp.LikeProperty) -> str:
        if self.SUPPORTS_CREATE_TABLE_LIKE:
            options = " ".join(f"{e.name} {self.sql(e, 'value')}" for e in expression.expressions)
            options = f" {options}" if options else ""

            like = f"LIKE {self.sql(expression, 'this')}{options}"
            if self.LIKE_PROPERTY_INSIDE_SCHEMA and not isinstance(expression.parent, exp.Schema):
                like = f"({like})"

            return like

        if expression.expressions:
            self.unsupported("Transpilation of LIKE property options is unsupported")

        select = exp.select("*").from_(expression.this).limit(0)
        return f"AS {self.sql(select)}"

    def fallbackproperty_sql(self, expression: exp.FallbackProperty) -> str:
        no = "NO " if expression.args.get("no") else ""
        protection = " PROTECTION" if expression.args.get("protection") else ""
        return f"{no}FALLBACK{protection}"

    def journalproperty_sql(self, expression: exp.JournalProperty) -> str:
        no = "NO " if expression.args.get("no") else ""
        local = expression.args.get("local")
        local = f"{local} " if local else ""
        dual = "DUAL " if expression.args.get("dual") else ""
        before = "BEFORE " if expression.args.get("before") else ""
        after = "AFTER " if expression.args.get("after") else ""
        return f"{no}{local}{dual}{before}{after}JOURNAL"

    def freespaceproperty_sql(self, expression: exp.FreespaceProperty) -> str:
        freespace = self.sql(expression, "this")
        percent = " PERCENT" if expression.args.get("percent") else ""
        return f"FREESPACE={freespace}{percent}"

    def checksumproperty_sql(self, expression: exp.ChecksumProperty) -> str:
        if expression.args.get("default"):
            property = "DEFAULT"
        elif expression.args.get("on"):
            property = "ON"
        else:
            property = "OFF"
        return f"CHECKSUM={property}"

    def mergeblockratioproperty_sql(self, expression: exp.MergeBlockRatioProperty) -> str:
        if expression.args.get("no"):
            return "NO MERGEBLOCKRATIO"
        if expression.args.get("default"):
            return "DEFAULT MERGEBLOCKRATIO"

        percent = " PERCENT" if expression.args.get("percent") else ""
        return f"MERGEBLOCKRATIO={self.sql(expression, 'this')}{percent}"

    def datablocksizeproperty_sql(self, expression: exp.DataBlocksizeProperty) -> str:
        default = expression.args.get("default")
        minimum = expression.args.get("minimum")
        maximum = expression.args.get("maximum")
        if default or minimum or maximum:
            if default:
                prop = "DEFAULT"
            elif minimum:
                prop = "MINIMUM"
            else:
                prop = "MAXIMUM"
            return f"{prop} DATABLOCKSIZE"
        units = expression.args.get("units")
        units = f" {units}" if units else ""
        return f"DATABLOCKSIZE={self.sql(expression, 'size')}{units}"

    def blockcompressionproperty_sql(self, expression: exp.BlockCompressionProperty) -> str:
        autotemp = expression.args.get("autotemp")
        always = expression.args.get("always")
        default = expression.args.get("default")
        manual = expression.args.get("manual")
        never = expression.args.get("never")

        if autotemp is not None:
            prop = f"AUTOTEMP({self.expressions(autotemp)})"
        elif always:
            prop = "ALWAYS"
        elif default:
            prop = "DEFAULT"
        elif manual:
            prop = "MANUAL"
        elif never:
            prop = "NEVER"
        return f"BLOCKCOMPRESSION={prop}"

    def isolatedloadingproperty_sql(self, expression: exp.IsolatedLoadingProperty) -> str:
        no = expression.args.get("no")
        no = " NO" if no else ""
        concurrent = expression.args.get("concurrent")
        concurrent = " CONCURRENT" if concurrent else ""

        for_ = ""
        if expression.args.get("for_all"):
            for_ = " FOR ALL"
        elif expression.args.get("for_insert"):
            for_ = " FOR INSERT"
        elif expression.args.get("for_none"):
            for_ = " FOR NONE"
        return f"WITH{no}{concurrent} ISOLATED LOADING{for_}"

    def partitionboundspec_sql(self, expression: exp.PartitionBoundSpec) -> str:
        if isinstance(expression.this, list):
            return f"IN ({self.expressions(expression, key='this', flat=True)})"
        if expression.this:
            modulus = self.sql(expression, "this")
            remainder = self.sql(expression, "expression")
            return f"WITH (MODULUS {modulus}, REMAINDER {remainder})"

        from_expressions = self.expressions(expression, key="from_expressions", flat=True)
        to_expressions = self.expressions(expression, key="to_expressions", flat=True)
        return f"FROM ({from_expressions}) TO ({to_expressions})"

    def partitionedofproperty_sql(self, expression: exp.PartitionedOfProperty) -> str:
        this = self.sql(expression, "this")

        for_values_or_default = expression.expression
        if isinstance(for_values_or_default, exp.PartitionBoundSpec):
            for_values_or_default = f" FOR VALUES {self.sql(for_values_or_default)}"
        else:
            for_values_or_default = " DEFAULT"

        return f"PARTITION OF {this}{for_values_or_default}"

    def lockingproperty_sql(self, expression: exp.LockingProperty) -> str:
        kind = expression.args.get("kind")
        this = f" {self.sql(expression, 'this')}" if expression.this else ""
        for_or_in = expression.args.get("for_or_in")
        for_or_in = f" {for_or_in}" if for_or_in else ""
        lock_type = expression.args.get("lock_type")
        override = " OVERRIDE" if expression.args.get("override") else ""
        return f"LOCKING {kind}{this}{for_or_in} {lock_type}{override}"

    def withdataproperty_sql(self, expression: exp.WithDataProperty) -> str:
        data_sql = f"WITH {'NO ' if expression.args.get('no') else ''}DATA"
        statistics = expression.args.get("statistics")
        statistics_sql = ""
        if statistics is not None:
            statistics_sql = f" AND {'NO ' if not statistics else ''}STATISTICS"
        return f"{data_sql}{statistics_sql}"

    def withsystemversioningproperty_sql(self, expression: exp.WithSystemVersioningProperty) -> str:
        sql = "WITH(SYSTEM_VERSIONING=ON"

        if expression.this:
            history_table = self.sql(expression, "this")
            sql = f"{sql}(HISTORY_TABLE={history_table}"

            if expression.expression:
                data_consistency_check = self.sql(expression, "expression")
                sql = f"{sql}, DATA_CONSISTENCY_CHECK={data_consistency_check}"

            sql = f"{sql})"

        return f"{sql})"

    def insert_sql(self, expression: exp.Insert) -> str:
        overwrite = expression.args.get("overwrite")

        if isinstance(expression.this, exp.Directory):
            this = " OVERWRITE" if overwrite else " INTO"
        else:
            this = self.INSERT_OVERWRITE if overwrite else " INTO"

        alternative = expression.args.get("alternative")
        alternative = f" OR {alternative}" if alternative else ""
        ignore = " IGNORE" if expression.args.get("ignore") else ""

        this = f"{this} {self.sql(expression, 'this')}"

        exists = " IF EXISTS" if expression.args.get("exists") else ""
        partition_sql = (
            f" {self.sql(expression, 'partition')}" if expression.args.get("partition") else ""
        )
        where = self.sql(expression, "where")
        where = f"{self.sep()}REPLACE WHERE {where}" if where else ""
        expression_sql = f"{self.sep()}{self.sql(expression, 'expression')}"
        conflict = self.sql(expression, "conflict")
        by_name = " BY NAME" if expression.args.get("by_name") else ""
        returning = self.sql(expression, "returning")

        if self.RETURNING_END:
            expression_sql = f"{expression_sql}{conflict}{returning}"
        else:
            expression_sql = f"{returning}{expression_sql}{conflict}"

        sql = f"INSERT{alternative}{ignore}{this}{by_name}{exists}{partition_sql}{where}{expression_sql}"
        return self.prepend_ctes(expression, sql)

    def intersect_sql(self, expression: exp.Intersect) -> str:
        return self.prepend_ctes(
            expression,
            self.set_operation(expression, self.intersect_op(expression)),
        )

    def intersect_op(self, expression: exp.Intersect) -> str:
        return f"INTERSECT{'' if expression.args.get('distinct') else ' ALL'}"

    def introducer_sql(self, expression: exp.Introducer) -> str:
        return f"{self.sql(expression, 'this')} {self.sql(expression, 'expression')}"

    def kill_sql(self, expression: exp.Kill) -> str:
        kind = self.sql(expression, "kind")
        kind = f" {kind}" if kind else ""
        this = self.sql(expression, "this")
        this = f" {this}" if this else ""
        return f"KILL{kind}{this}"

    def pseudotype_sql(self, expression: exp.PseudoType) -> str:
        return expression.name

    def objectidentifier_sql(self, expression: exp.ObjectIdentifier) -> str:
        return expression.name

    def onconflict_sql(self, expression: exp.OnConflict) -> str:
        conflict = "ON DUPLICATE KEY" if expression.args.get("duplicate") else "ON CONFLICT"
        constraint = self.sql(expression, "constraint")
        if constraint:
            constraint = f"ON CONSTRAINT {constraint}"
        key = self.expressions(expression, key="key", flat=True)
        do = "" if expression.args.get("duplicate") else " DO "
        nothing = "NOTHING" if expression.args.get("nothing") else ""
        expressions = self.expressions(expression, flat=True)
        set_keyword = "SET " if self.DUPLICATE_KEY_UPDATE_WITH_SET else ""
        if expressions:
            expressions = f"UPDATE {set_keyword}{expressions}"
        return f"{self.seg(conflict)} {constraint}{key}{do}{nothing}{expressions}"

    def returning_sql(self, expression: exp.Returning) -> str:
        return f"{self.seg('RETURNING')} {self.expressions(expression, flat=True)}"

    def rowformatdelimitedproperty_sql(self, expression: exp.RowFormatDelimitedProperty) -> str:
        fields = expression.args.get("fields")
        fields = f" FIELDS TERMINATED BY {fields}" if fields else ""
        escaped = expression.args.get("escaped")
        escaped = f" ESCAPED BY {escaped}" if escaped else ""
        items = expression.args.get("collection_items")
        items = f" COLLECTION ITEMS TERMINATED BY {items}" if items else ""
        keys = expression.args.get("map_keys")
        keys = f" MAP KEYS TERMINATED BY {keys}" if keys else ""
        lines = expression.args.get("lines")
        lines = f" LINES TERMINATED BY {lines}" if lines else ""
        null = expression.args.get("null")
        null = f" NULL DEFINED AS {null}" if null else ""
        return f"ROW FORMAT DELIMITED{fields}{escaped}{items}{keys}{lines}{null}"

    def withtablehint_sql(self, expression: exp.WithTableHint) -> str:
        return f"WITH ({self.expressions(expression, flat=True)})"

    def indextablehint_sql(self, expression: exp.IndexTableHint) -> str:
        this = f"{self.sql(expression, 'this')} INDEX"
        target = self.sql(expression, "target")
        target = f" FOR {target}" if target else ""
        return f"{this}{target} ({self.expressions(expression, flat=True)})"

    def historicaldata_sql(self, expression: exp.HistoricalData) -> str:
        this = self.sql(expression, "this")
        kind = self.sql(expression, "kind")
        expr = self.sql(expression, "expression")
        return f"{this} ({kind} => {expr})"

    def table_sql(self, expression: exp.Table, sep: str = " AS ") -> str:
        table = ".".join(
            self.sql(part)
            for part in (
                expression.args.get("catalog"),
                expression.args.get("db"),
                expression.args.get("this"),
            )
            if part is not None
        )

        version = self.sql(expression, "version")
        version = f" {version}" if version else ""
        alias = self.sql(expression, "alias")
        alias = f"{sep}{alias}" if alias else ""
        hints = self.expressions(expression, key="hints", sep=" ")
        hints = f" {hints}" if hints and self.TABLE_HINTS else ""
        pivots = self.expressions(expression, key="pivots", sep=" ", flat=True)
        pivots = f" {pivots}" if pivots else ""
        joins = self.expressions(expression, key="joins", sep="", skip_first=True)
        laterals = self.expressions(expression, key="laterals", sep="")

        file_format = self.sql(expression, "format")
        if file_format:
            pattern = self.sql(expression, "pattern")
            pattern = f", PATTERN => {pattern}" if pattern else ""
            file_format = f" (FILE_FORMAT => {file_format}{pattern})"

        ordinality = expression.args.get("ordinality") or ""
        if ordinality:
            ordinality = f" WITH ORDINALITY{alias}"
            alias = ""

        when = self.sql(expression, "when")
        if when:
            table = f"{table} {when}"

        return f"{table}{version}{file_format}{alias}{hints}{pivots}{joins}{laterals}{ordinality}"

    def tablesample_sql(
        self,
        expression: exp.TableSample,
        sep: str = " AS ",
        tablesample_keyword: t.Optional[str] = None,
    ) -> str:
        if self.dialect.ALIAS_POST_TABLESAMPLE and expression.this and expression.this.alias:
            table = expression.this.copy()
            table.set("alias", None)
            this = self.sql(table)
            alias = f"{sep}{self.sql(expression.this, 'alias')}"
        else:
            this = self.sql(expression, "this")
            alias = ""

        method = self.sql(expression, "method")
        method = f"{method} " if method and self.TABLESAMPLE_WITH_METHOD else ""
        numerator = self.sql(expression, "bucket_numerator")
        denominator = self.sql(expression, "bucket_denominator")
        field = self.sql(expression, "bucket_field")
        field = f" ON {field}" if field else ""
        bucket = f"BUCKET {numerator} OUT OF {denominator}{field}" if numerator else ""
        seed = self.sql(expression, "seed")
        seed = f" {self.TABLESAMPLE_SEED_KEYWORD} ({seed})" if seed else ""

        size = self.sql(expression, "size")
        if size and self.TABLESAMPLE_SIZE_IS_ROWS:
            size = f"{size} ROWS"

        percent = self.sql(expression, "percent")
        if percent and not self.dialect.TABLESAMPLE_SIZE_IS_PERCENT:
            percent = f"{percent} PERCENT"

        expr = f"{bucket}{percent}{size}"
        if self.TABLESAMPLE_REQUIRES_PARENS:
            expr = f"({expr})"

        return (
            f"{this} {tablesample_keyword or self.TABLESAMPLE_KEYWORDS} {method}{expr}{seed}{alias}"
        )

    def pivot_sql(self, expression: exp.Pivot) -> str:
        expressions = self.expressions(expression, flat=True)

        if expression.this:
            this = self.sql(expression, "this")
            if not expressions:
                return f"UNPIVOT {this}"

            on = f"{self.seg('ON')} {expressions}"
            using = self.expressions(expression, key="using", flat=True)
            using = f"{self.seg('USING')} {using}" if using else ""
            group = self.sql(expression, "group")
            return f"PIVOT {this}{on}{using}{group}"

        alias = self.sql(expression, "alias")
        alias = f" AS {alias}" if alias else ""
        direction = "UNPIVOT" if expression.unpivot else "PIVOT"
        field = self.sql(expression, "field")
        include_nulls = expression.args.get("include_nulls")
        if include_nulls is not None:
            nulls = " INCLUDE NULLS " if include_nulls else " EXCLUDE NULLS "
        else:
            nulls = ""
        return f"{direction}{nulls}({expressions} FOR {field}){alias}"

    def version_sql(self, expression: exp.Version) -> str:
        this = f"FOR {expression.name}"
        kind = expression.text("kind")
        expr = self.sql(expression, "expression")
        return f"{this} {kind} {expr}"

    def tuple_sql(self, expression: exp.Tuple) -> str:
        return f"({self.expressions(expression, flat=True)})"

    def update_sql(self, expression: exp.Update) -> str:
        this = self.sql(expression, "this")
        set_sql = self.expressions(expression, flat=True)
        from_sql = self.sql(expression, "from")
        where_sql = self.sql(expression, "where")
        returning = self.sql(expression, "returning")
        order = self.sql(expression, "order")
        limit = self.sql(expression, "limit")
        if self.RETURNING_END:
            expression_sql = f"{from_sql}{where_sql}{returning}"
        else:
            expression_sql = f"{returning}{from_sql}{where_sql}"
        sql = f"UPDATE {this} SET {set_sql}{expression_sql}{order}{limit}"
        return self.prepend_ctes(expression, sql)

    def values_sql(self, expression: exp.Values) -> str:
        # The VALUES clause is still valid in an `INSERT INTO ..` statement, for example
        if self.VALUES_AS_TABLE or not expression.find_ancestor(exp.From, exp.Join):
            args = self.expressions(expression)
            alias = self.sql(expression, "alias")
            values = f"VALUES{self.seg('')}{args}"
            values = (
                f"({values})"
                if self.WRAP_DERIVED_VALUES and (alias or isinstance(expression.parent, exp.From))
                else values
            )
            return f"{values} AS {alias}" if alias else values

        # Converts `VALUES...` expression into a series of select unions.
        alias_node = expression.args.get("alias")
        column_names = alias_node and alias_node.columns

        selects: t.List[exp.Subqueryable] = []

        for i, tup in enumerate(expression.expressions):
            row = tup.expressions

            if i == 0 and column_names:
                row = [
                    exp.alias_(value, column_name) for value, column_name in zip(row, column_names)
                ]

            selects.append(exp.Select(expressions=row))

        if self.pretty:
            # This may result in poor performance for large-cardinality `VALUES` tables, due to
            # the deep nesting of the resulting exp.Unions. If this is a problem, either increase
            # `sys.setrecursionlimit` to avoid RecursionErrors, or don't set `pretty`.
            subqueryable = reduce(lambda x, y: exp.union(x, y, distinct=False, copy=False), selects)
            return self.subquery_sql(
                subqueryable.subquery(alias_node and alias_node.this, copy=False)
            )

        alias = f" AS {self.sql(alias_node, 'this')}" if alias_node else ""
        unions = " UNION ALL ".join(self.sql(select) for select in selects)
        return f"({unions}){alias}"

    def var_sql(self, expression: exp.Var) -> str:
        return self.sql(expression, "this")

    def into_sql(self, expression: exp.Into) -> str:
        temporary = " TEMPORARY" if expression.args.get("temporary") else ""
        unlogged = " UNLOGGED" if expression.args.get("unlogged") else ""
        return f"{self.seg('INTO')}{temporary or unlogged} {self.sql(expression, 'this')}"

    def from_sql(self, expression: exp.From) -> str:
        return f"{self.seg('FROM')} {self.sql(expression, 'this')}"

    def group_sql(self, expression: exp.Group) -> str:
        group_by = self.op_expressions("GROUP BY", expression)

        if expression.args.get("all"):
            return f"{group_by} ALL"

        grouping_sets = self.expressions(expression, key="grouping_sets", indent=False)
        grouping_sets = (
            f"{self.seg('GROUPING SETS')} {self.wrap(grouping_sets)}" if grouping_sets else ""
        )

        cube = expression.args.get("cube", [])
        if seq_get(cube, 0) is True:
            return f"{group_by}{self.seg('WITH CUBE')}"
        else:
            cube_sql = self.expressions(expression, key="cube", indent=False)
            cube_sql = f"{self.seg('CUBE')} {self.wrap(cube_sql)}" if cube_sql else ""

        rollup = expression.args.get("rollup", [])
        if seq_get(rollup, 0) is True:
            return f"{group_by}{self.seg('WITH ROLLUP')}"
        else:
            rollup_sql = self.expressions(expression, key="rollup", indent=False)
            rollup_sql = f"{self.seg('ROLLUP')} {self.wrap(rollup_sql)}" if rollup_sql else ""

        groupings = csv(
            grouping_sets,
            cube_sql,
            rollup_sql,
            self.seg("WITH TOTALS") if expression.args.get("totals") else "",
            sep=self.GROUPINGS_SEP,
        )

        if expression.args.get("expressions") and groupings:
            group_by = f"{group_by}{self.GROUPINGS_SEP}"

        return f"{group_by}{groupings}"

    def having_sql(self, expression: exp.Having) -> str:
        this = self.indent(self.sql(expression, "this"))
        return f"{self.seg('HAVING')}{self.sep()}{this}"

    def connect_sql(self, expression: exp.Connect) -> str:
        start = self.sql(expression, "start")
        start = self.seg(f"START WITH {start}") if start else ""
        connect = self.sql(expression, "connect")
        connect = self.seg(f"CONNECT BY {connect}")
        return start + connect

    def prior_sql(self, expression: exp.Prior) -> str:
        return f"PRIOR {self.sql(expression, 'this')}"

    def join_sql(self, expression: exp.Join) -> str:
        if not self.SEMI_ANTI_JOIN_WITH_SIDE and expression.kind in ("SEMI", "ANTI"):
            side = None
        else:
            side = expression.side

        op_sql = " ".join(
            op
            for op in (
                expression.method,
                "GLOBAL" if expression.args.get("global") else None,
                side,
                expression.kind,
                expression.hint if self.JOIN_HINTS else None,
            )
            if op
        )
        on_sql = self.sql(expression, "on")
        using = expression.args.get("using")

        if not on_sql and using:
            on_sql = csv(*(self.sql(column) for column in using))

        this = expression.this
        this_sql = self.sql(this)

        if on_sql:
            on_sql = self.indent(on_sql, skip_first=True)
            space = self.seg(" " * self.pad) if self.pretty else " "
            if using:
                on_sql = f"{space}USING ({on_sql})"
            else:
                on_sql = f"{space}ON {on_sql}"
        elif not op_sql:
            if isinstance(this, exp.Lateral) and this.args.get("cross_apply") is not None:
                return f" {this_sql}"

            return f", {this_sql}"

        op_sql = f"{op_sql} JOIN" if op_sql else "JOIN"
        return f"{self.seg(op_sql)} {this_sql}{on_sql}"

    def lambda_sql(self, expression: exp.Lambda, arrow_sep: str = "->") -> str:
        args = self.expressions(expression, flat=True)
        args = f"({args})" if len(args.split(",")) > 1 else args
        return f"{args} {arrow_sep} {self.sql(expression, 'this')}"

    def lateral_op(self, expression: exp.Lateral) -> str:
        cross_apply = expression.args.get("cross_apply")

        # https://www.mssqltips.com/sqlservertip/1958/sql-server-cross-apply-and-outer-apply/
        if cross_apply is True:
            op = "INNER JOIN "
        elif cross_apply is False:
            op = "LEFT JOIN "
        else:
            op = ""

        return f"{op}LATERAL"

    def lateral_sql(self, expression: exp.Lateral) -> str:
        this = self.sql(expression, "this")

        if expression.args.get("view"):
            alias = expression.args["alias"]
            columns = self.expressions(alias, key="columns", flat=True)
            table = f" {alias.name}" if alias.name else ""
            columns = f" AS {columns}" if columns else ""
            op_sql = self.seg(f"LATERAL VIEW{' OUTER' if expression.args.get('outer') else ''}")
            return f"{op_sql}{self.sep()}{this}{table}{columns}"

        alias = self.sql(expression, "alias")
        alias = f" AS {alias}" if alias else ""
        return f"{self.lateral_op(expression)} {this}{alias}"

    def limit_sql(self, expression: exp.Limit, top: bool = False) -> str:
        this = self.sql(expression, "this")

        args = [
            self._simplify_unless_literal(e) if self.LIMIT_ONLY_LITERALS else e
            for e in (expression.args.get(k) for k in ("offset", "expression"))
            if e
        ]

        args_sql = ", ".join(self.sql(e) for e in args)
        args_sql = f"({args_sql})" if any(top and not e.is_number for e in args) else args_sql
        expressions = self.expressions(expression, flat=True)
        expressions = f" BY {expressions}" if expressions else ""

        return f"{this}{self.seg('TOP' if top else 'LIMIT')} {args_sql}{expressions}"

    def offset_sql(self, expression: exp.Offset) -> str:
        this = self.sql(expression, "this")
        value = expression.expression
        value = self._simplify_unless_literal(value) if self.LIMIT_ONLY_LITERALS else value
        expressions = self.expressions(expression, flat=True)
        expressions = f" BY {expressions}" if expressions else ""
        return f"{this}{self.seg('OFFSET')} {self.sql(value)}{expressions}"

    def setitem_sql(self, expression: exp.SetItem) -> str:
        kind = self.sql(expression, "kind")
        kind = f"{kind} " if kind else ""
        this = self.sql(expression, "this")
        expressions = self.expressions(expression)
        collate = self.sql(expression, "collate")
        collate = f" COLLATE {collate}" if collate else ""
        global_ = "GLOBAL " if expression.args.get("global") else ""
        return f"{global_}{kind}{this}{expressions}{collate}"

    def set_sql(self, expression: exp.Set) -> str:
        expressions = (
            f" {self.expressions(expression, flat=True)}" if expression.expressions else ""
        )
        tag = " TAG" if expression.args.get("tag") else ""
        return f"{'UNSET' if expression.args.get('unset') else 'SET'}{tag}{expressions}"

    def pragma_sql(self, expression: exp.Pragma) -> str:
        return f"PRAGMA {self.sql(expression, 'this')}"

    def lock_sql(self, expression: exp.Lock) -> str:
        if not self.LOCKING_READS_SUPPORTED:
            self.unsupported("Locking reads using 'FOR UPDATE/SHARE' are not supported")
            return ""

        lock_type = "FOR UPDATE" if expression.args["update"] else "FOR SHARE"
        expressions = self.expressions(expression, flat=True)
        expressions = f" OF {expressions}" if expressions else ""
        wait = expression.args.get("wait")

        if wait is not None:
            if isinstance(wait, exp.Literal):
                wait = f" WAIT {self.sql(wait)}"
            else:
                wait = " NOWAIT" if wait else " SKIP LOCKED"

        return f"{lock_type}{expressions}{wait or ''}"

    def literal_sql(self, expression: exp.Literal) -> str:
        text = expression.this or ""
        if expression.is_string:
            text = f"{self.dialect.QUOTE_START}{self.escape_str(text)}{self.dialect.QUOTE_END}"
        return text

    def escape_str(self, text: str) -> str:
        text = text.replace(self.dialect.QUOTE_END, self._escaped_quote_end)
        if self.dialect.INVERSE_ESCAPE_SEQUENCES:
            text = "".join(self.dialect.INVERSE_ESCAPE_SEQUENCES.get(ch, ch) for ch in text)
        elif self.pretty:
            text = text.replace("\n", self.SENTINEL_LINE_BREAK)
        return text

    def loaddata_sql(self, expression: exp.LoadData) -> str:
        local = " LOCAL" if expression.args.get("local") else ""
        inpath = f" INPATH {self.sql(expression, 'inpath')}"
        overwrite = " OVERWRITE" if expression.args.get("overwrite") else ""
        this = f" INTO TABLE {self.sql(expression, 'this')}"
        partition = self.sql(expression, "partition")
        partition = f" {partition}" if partition else ""
        input_format = self.sql(expression, "input_format")
        input_format = f" INPUTFORMAT {input_format}" if input_format else ""
        serde = self.sql(expression, "serde")
        serde = f" SERDE {serde}" if serde else ""
        return f"LOAD DATA{local}{inpath}{overwrite}{this}{partition}{input_format}{serde}"

    def null_sql(self, *_) -> str:
        return "NULL"

    def boolean_sql(self, expression: exp.Boolean) -> str:
        return "TRUE" if expression.this else "FALSE"

    def order_sql(self, expression: exp.Order, flat: bool = False) -> str:
        this = self.sql(expression, "this")
        this = f"{this} " if this else this
        siblings = "SIBLINGS " if expression.args.get("siblings") else ""
        order = self.op_expressions(f"{this}ORDER {siblings}BY", expression, flat=this or flat)  # type: ignore
        interpolated_values = [
            f"{self.sql(named_expression, 'alias')} AS {self.sql(named_expression, 'this')}"
            for named_expression in expression.args.get("interpolate") or []
        ]
        interpolate = (
            f" INTERPOLATE ({', '.join(interpolated_values)})" if interpolated_values else ""
        )
        return f"{order}{interpolate}"

    def withfill_sql(self, expression: exp.WithFill) -> str:
        from_sql = self.sql(expression, "from")
        from_sql = f" FROM {from_sql}" if from_sql else ""
        to_sql = self.sql(expression, "to")
        to_sql = f" TO {to_sql}" if to_sql else ""
        step_sql = self.sql(expression, "step")
        step_sql = f" STEP {step_sql}" if step_sql else ""
        return f"WITH FILL{from_sql}{to_sql}{step_sql}"

    def cluster_sql(self, expression: exp.Cluster) -> str:
        return self.op_expressions("CLUSTER BY", expression)

    def distribute_sql(self, expression: exp.Distribute) -> str:
        return self.op_expressions("DISTRIBUTE BY", expression)

    def sort_sql(self, expression: exp.Sort) -> str:
        return self.op_expressions("SORT BY", expression)

    def ordered_sql(self, expression: exp.Ordered) -> str:
        desc = expression.args.get("desc")
        asc = not desc

        nulls_first = expression.args.get("nulls_first")
        nulls_last = not nulls_first
        nulls_are_large = self.dialect.NULL_ORDERING == "nulls_are_large"
        nulls_are_small = self.dialect.NULL_ORDERING == "nulls_are_small"
        nulls_are_last = self.dialect.NULL_ORDERING == "nulls_are_last"

        this = self.sql(expression, "this")

        sort_order = " DESC" if desc else (" ASC" if desc is False else "")
        nulls_sort_change = ""
        if nulls_first and (
            (asc and nulls_are_large) or (desc and nulls_are_small) or nulls_are_last
        ):
            nulls_sort_change = " NULLS FIRST"
        elif (
            nulls_last
            and ((asc and nulls_are_small) or (desc and nulls_are_large))
            and not nulls_are_last
        ):
            nulls_sort_change = " NULLS LAST"

        # If the NULLS FIRST/LAST clause is unsupported, we add another sort key to simulate it
        if nulls_sort_change and not self.NULL_ORDERING_SUPPORTED:
            window = expression.find_ancestor(exp.Window, exp.Select)
            if isinstance(window, exp.Window) and window.args.get("spec"):
                self.unsupported(
                    f"'{nulls_sort_change.strip()}' translation not supported in window functions"
                )
                nulls_sort_change = ""
            elif self.NULL_ORDERING_SUPPORTED is None:
                if expression.this.is_int:
                    self.unsupported(
                        f"'{nulls_sort_change.strip()}' translation not supported with positional ordering"
                    )
                else:
                    null_sort_order = " DESC" if nulls_sort_change == " NULLS FIRST" else ""
                    this = f"CASE WHEN {this} IS NULL THEN 1 ELSE 0 END{null_sort_order}, {this}"
                nulls_sort_change = ""

        with_fill = self.sql(expression, "with_fill")
        with_fill = f" {with_fill}" if with_fill else ""

        return f"{this}{sort_order}{nulls_sort_change}{with_fill}"

    def matchrecognize_sql(self, expression: exp.MatchRecognize) -> str:
        partition = self.partition_by_sql(expression)
        order = self.sql(expression, "order")
        measures = self.expressions(expression, key="measures")
        measures = self.seg(f"MEASURES{self.seg(measures)}") if measures else ""
        rows = self.sql(expression, "rows")
        rows = self.seg(rows) if rows else ""
        after = self.sql(expression, "after")
        after = self.seg(after) if after else ""
        pattern = self.sql(expression, "pattern")
        pattern = self.seg(f"PATTERN ({pattern})") if pattern else ""
        definition_sqls = [
            f"{self.sql(definition, 'alias')} AS {self.sql(definition, 'this')}"
            for definition in expression.args.get("define", [])
        ]
        definitions = self.expressions(sqls=definition_sqls)
        define = self.seg(f"DEFINE{self.seg(definitions)}") if definitions else ""
        body = "".join(
            (
                partition,
                order,
                measures,
                rows,
                after,
                pattern,
                define,
            )
        )
        alias = self.sql(expression, "alias")
        alias = f" {alias}" if alias else ""
        return f"{self.seg('MATCH_RECOGNIZE')} {self.wrap(body)}{alias}"

    def query_modifiers(self, expression: exp.Expression, *sqls: str) -> str:
        limit: t.Optional[exp.Fetch | exp.Limit] = expression.args.get("limit")

        # If the limit is generated as TOP, we need to ensure it's not generated twice
        with_offset_limit_modifiers = not isinstance(limit, exp.Limit) or not self.LIMIT_IS_TOP

        if self.LIMIT_FETCH == "LIMIT" and isinstance(limit, exp.Fetch):
            limit = exp.Limit(expression=exp.maybe_copy(limit.args.get("count")))
        elif self.LIMIT_FETCH == "FETCH" and isinstance(limit, exp.Limit):
            limit = exp.Fetch(direction="FIRST", count=exp.maybe_copy(limit.expression))

        fetch = isinstance(limit, exp.Fetch)

        offset_limit_modifiers = (
            self.offset_limit_modifiers(expression, fetch, limit)
            if with_offset_limit_modifiers
            else []
        )

        return csv(
            *sqls,
            *[self.sql(join) for join in expression.args.get("joins") or []],
            self.sql(expression, "connect"),
            self.sql(expression, "match"),
            *[self.sql(lateral) for lateral in expression.args.get("laterals") or []],
            self.sql(expression, "where"),
            self.sql(expression, "group"),
            self.sql(expression, "having"),
            *self.after_having_modifiers(expression),
            self.sql(expression, "order"),
            *offset_limit_modifiers,
            *self.after_limit_modifiers(expression),
            sep="",
        )

    def offset_limit_modifiers(
        self, expression: exp.Expression, fetch: bool, limit: t.Optional[exp.Fetch | exp.Limit]
    ) -> t.List[str]:
        return [
            self.sql(expression, "offset") if fetch else self.sql(limit),
            self.sql(limit) if fetch else self.sql(expression, "offset"),
        ]

    def after_having_modifiers(self, expression: exp.Expression) -> t.List[str]:
        return [
            self.sql(expression, "qualify"),
            (
                self.seg("WINDOW ") + self.expressions(expression, key="windows", flat=True)
                if expression.args.get("windows")
                else ""
            ),
            self.sql(expression, "distribute"),
            self.sql(expression, "sort"),
            self.sql(expression, "cluster"),
        ]

    def after_limit_modifiers(self, expression: exp.Expression) -> t.List[str]:
        locks = self.expressions(expression, key="locks", sep=" ")
        locks = f" {locks}" if locks else ""
        return [locks, self.sql(expression, "sample")]

    def select_sql(self, expression: exp.Select) -> str:
        into = expression.args.get("into")
        if not self.SUPPORTS_SELECT_INTO and into:
            into.pop()

        hint = self.sql(expression, "hint")
        distinct = self.sql(expression, "distinct")
        distinct = f" {distinct}" if distinct else ""
        kind = self.sql(expression, "kind")
        limit = expression.args.get("limit")
        top = (
            self.limit_sql(limit, top=True)
            if isinstance(limit, exp.Limit) and self.LIMIT_IS_TOP
            else ""
        )

        expressions = self.expressions(expression)

        if kind:
            if kind in self.SELECT_KINDS:
                kind = f" AS {kind}"
            else:
                if kind == "STRUCT":
                    expressions = self.expressions(
                        sqls=[
                            self.sql(
                                exp.Struct(
                                    expressions=[
                                        exp.column(e.output_name).eq(
                                            e.this if isinstance(e, exp.Alias) else e
                                        )
                                        for e in expression.expressions
                                    ]
                                )
                            )
                        ]
                    )
                kind = ""

        # We use LIMIT_IS_TOP as a proxy for whether DISTINCT should go first because tsql and Teradata
        # are the only dialects that use LIMIT_IS_TOP and both place DISTINCT first.
        top_distinct = f"{distinct}{hint}{top}" if self.LIMIT_IS_TOP else f"{top}{hint}{distinct}"
        expressions = f"{self.sep()}{expressions}" if expressions else expressions
        sql = self.query_modifiers(
            expression,
            f"SELECT{top_distinct}{kind}{expressions}",
            self.sql(expression, "into", comment=False),
            self.sql(expression, "from", comment=False),
        )

        sql = self.prepend_ctes(expression, sql)

        if not self.SUPPORTS_SELECT_INTO and into:
            if into.args.get("temporary"):
                table_kind = " TEMPORARY"
            elif self.SUPPORTS_UNLOGGED_TABLES and into.args.get("unlogged"):
                table_kind = " UNLOGGED"
            else:
                table_kind = ""
            sql = f"CREATE{table_kind} TABLE {self.sql(into.this)} AS {sql}"

        return sql

    def schema_sql(self, expression: exp.Schema) -> str:
        this = self.sql(expression, "this")
        sql = self.schema_columns_sql(expression)
        return f"{this} {sql}" if this and sql else this or sql

    def schema_columns_sql(self, expression: exp.Schema) -> str:
        if expression.expressions:
            return f"({self.sep('')}{self.expressions(expression)}{self.seg(')', sep='')}"
        return ""

    def star_sql(self, expression: exp.Star) -> str:
        except_ = self.expressions(expression, key="except", flat=True)
        except_ = f"{self.seg(self.STAR_MAPPING['except'])} ({except_})" if except_ else ""
        replace = self.expressions(expression, key="replace", flat=True)
        replace = f"{self.seg(self.STAR_MAPPING['replace'])} ({replace})" if replace else ""
        return f"*{except_}{replace}"

    def parameter_sql(self, expression: exp.Parameter) -> str:
        this = self.sql(expression, "this")
        return f"{self.PARAMETER_TOKEN}{this}"

    def sessionparameter_sql(self, expression: exp.SessionParameter) -> str:
        this = self.sql(expression, "this")
        kind = expression.text("kind")
        if kind:
            kind = f"{kind}."
        return f"@@{kind}{this}"

    def placeholder_sql(self, expression: exp.Placeholder) -> str:
        return f":{expression.name}" if expression.name else "?"

    def subquery_sql(self, expression: exp.Subquery, sep: str = " AS ") -> str:
        alias = self.sql(expression, "alias")
        alias = f"{sep}{alias}" if alias else ""

        pivots = self.expressions(expression, key="pivots", sep=" ", flat=True)
        pivots = f" {pivots}" if pivots else ""

        sql = self.query_modifiers(expression, self.wrap(expression), alias, pivots)
        return self.prepend_ctes(expression, sql)

    def qualify_sql(self, expression: exp.Qualify) -> str:
        this = self.indent(self.sql(expression, "this"))
        return f"{self.seg('QUALIFY')}{self.sep()}{this}"

    def union_sql(self, expression: exp.Union) -> str:
        return self.prepend_ctes(
            expression,
            self.set_operation(expression, self.union_op(expression)),
        )

    def union_op(self, expression: exp.Union) -> str:
        kind = " DISTINCT" if self.EXPLICIT_UNION else ""
        kind = kind if expression.args.get("distinct") else " ALL"
        by_name = " BY NAME" if expression.args.get("by_name") else ""
        return f"UNION{kind}{by_name}"

    def unnest_sql(self, expression: exp.Unnest) -> str:
        args = self.expressions(expression, flat=True)

        alias = expression.args.get("alias")
        offset = expression.args.get("offset")

        if self.UNNEST_WITH_ORDINALITY:
            if alias and isinstance(offset, exp.Expression):
                alias.append("columns", offset)

        if alias and self.dialect.UNNEST_COLUMN_ONLY:
            columns = alias.columns
            alias = self.sql(columns[0]) if columns else ""
        else:
            alias = self.sql(alias)

        alias = f" AS {alias}" if alias else alias
        if self.UNNEST_WITH_ORDINALITY:
            suffix = f" WITH ORDINALITY{alias}" if offset else alias
        else:
            if isinstance(offset, exp.Expression):
                suffix = f"{alias} WITH OFFSET AS {self.sql(offset)}"
            elif offset:
                suffix = f"{alias} WITH OFFSET"
            else:
                suffix = alias

        return f"UNNEST({args}){suffix}"

    def where_sql(self, expression: exp.Where) -> str:
        this = self.indent(self.sql(expression, "this"))
        return f"{self.seg('WHERE')}{self.sep()}{this}"

    def window_sql(self, expression: exp.Window) -> str:
        this = self.sql(expression, "this")
        partition = self.partition_by_sql(expression)
        order = expression.args.get("order")
        order = self.order_sql(order, flat=True) if order else ""
        spec = self.sql(expression, "spec")
        alias = self.sql(expression, "alias")
        over = self.sql(expression, "over") or "OVER"

        this = f"{this} {'AS' if expression.arg_key == 'windows' else over}"

        first = expression.args.get("first")
        if first is None:
            first = ""
        else:
            first = "FIRST" if first else "LAST"

        if not partition and not order and not spec and alias:
            return f"{this} {alias}"

        args = " ".join(arg for arg in (alias, first, partition, order, spec) if arg)
        return f"{this} ({args})"

    def partition_by_sql(self, expression: exp.Window | exp.MatchRecognize) -> str:
        partition = self.expressions(expression, key="partition_by", flat=True)
        return f"PARTITION BY {partition}" if partition else ""

    def windowspec_sql(self, expression: exp.WindowSpec) -> str:
        kind = self.sql(expression, "kind")
        start = csv(self.sql(expression, "start"), self.sql(expression, "start_side"), sep=" ")
        end = (
            csv(self.sql(expression, "end"), self.sql(expression, "end_side"), sep=" ")
            or "CURRENT ROW"
        )
        return f"{kind} BETWEEN {start} AND {end}"

    def withingroup_sql(self, expression: exp.WithinGroup) -> str:
        this = self.sql(expression, "this")
        expression_sql = self.sql(expression, "expression")[1:]  # order has a leading space
        return f"{this} WITHIN GROUP ({expression_sql})"

    def between_sql(self, expression: exp.Between) -> str:
        this = self.sql(expression, "this")
        low = self.sql(expression, "low")
        high = self.sql(expression, "high")
        return f"{this} BETWEEN {low} AND {high}"

    def bracket_sql(self, expression: exp.Bracket) -> str:
        expressions = apply_index_offset(
            expression.this,
            expression.expressions,
            self.dialect.INDEX_OFFSET - expression.args.get("offset", 0),
        )
        expressions_sql = ", ".join(self.sql(e) for e in expressions)
        return f"{self.sql(expression, 'this')}[{expressions_sql}]"

    def all_sql(self, expression: exp.All) -> str:
        return f"ALL {self.wrap(expression)}"

    def any_sql(self, expression: exp.Any) -> str:
        this = self.sql(expression, "this")
        if isinstance(expression.this, exp.Subqueryable):
            this = self.wrap(this)
        return f"ANY {this}"

    def exists_sql(self, expression: exp.Exists) -> str:
        return f"EXISTS{self.wrap(expression)}"

    def case_sql(self, expression: exp.Case) -> str:
        this = self.sql(expression, "this")
        statements = [f"CASE {this}" if this else "CASE"]

        for e in expression.args["ifs"]:
            statements.append(f"WHEN {self.sql(e, 'this')}")
            statements.append(f"THEN {self.sql(e, 'true')}")

        default = self.sql(expression, "default")

        if default:
            statements.append(f"ELSE {default}")

        statements.append("END")

        if self.pretty and self.text_width(statements) > self.max_text_width:
            return self.indent("\n".join(statements), skip_first=True, skip_last=True)

        return " ".join(statements)

    def constraint_sql(self, expression: exp.Constraint) -> str:
        this = self.sql(expression, "this")
        expressions = self.expressions(expression, flat=True)
        return f"CONSTRAINT {this} {expressions}"

    def nextvaluefor_sql(self, expression: exp.NextValueFor) -> str:
        order = expression.args.get("order")
        order = f" OVER ({self.order_sql(order, flat=True)})" if order else ""
        return f"NEXT VALUE FOR {self.sql(expression, 'this')}{order}"

    def extract_sql(self, expression: exp.Extract) -> str:
        this = self.sql(expression, "this") if self.EXTRACT_ALLOWS_QUOTES else expression.this.name
        expression_sql = self.sql(expression, "expression")
        return f"EXTRACT({this} FROM {expression_sql})"

    def trim_sql(self, expression: exp.Trim) -> str:
        trim_type = self.sql(expression, "position")

        if trim_type == "LEADING":
            return self.func("LTRIM", expression.this)
        elif trim_type == "TRAILING":
            return self.func("RTRIM", expression.this)
        else:
            return self.func("TRIM", expression.this, expression.expression)

    def convert_concat_args(self, expression: exp.Concat | exp.ConcatWs) -> t.List[exp.Expression]:
        args = expression.expressions
        if isinstance(expression, exp.ConcatWs):
            args = args[1:]  # Skip the delimiter

        if self.dialect.STRICT_STRING_CONCAT and expression.args.get("safe"):
            args = [exp.cast(e, "text") for e in args]

        if not self.dialect.CONCAT_COALESCE and expression.args.get("coalesce"):
            args = [exp.func("coalesce", e, exp.Literal.string("")) for e in args]

        return args

    def concat_sql(self, expression: exp.Concat) -> str:
        expressions = self.convert_concat_args(expression)

        # Some dialects don't allow a single-argument CONCAT call
        if not self.SUPPORTS_SINGLE_ARG_CONCAT and len(expressions) == 1:
            return self.sql(expressions[0])

        return self.func("CONCAT", *expressions)

    def concatws_sql(self, expression: exp.ConcatWs) -> str:
        return self.func(
            "CONCAT_WS", seq_get(expression.expressions, 0), *self.convert_concat_args(expression)
        )

    def check_sql(self, expression: exp.Check) -> str:
        this = self.sql(expression, key="this")
        return f"CHECK ({this})"

    def foreignkey_sql(self, expression: exp.ForeignKey) -> str:
        expressions = self.expressions(expression, flat=True)
        reference = self.sql(expression, "reference")
        reference = f" {reference}" if reference else ""
        delete = self.sql(expression, "delete")
        delete = f" ON DELETE {delete}" if delete else ""
        update = self.sql(expression, "update")
        update = f" ON UPDATE {update}" if update else ""
        return f"FOREIGN KEY ({expressions}){reference}{delete}{update}"

    def primarykey_sql(self, expression: exp.ForeignKey) -> str:
        expressions = self.expressions(expression, flat=True)
        options = self.expressions(expression, key="options", flat=True, sep=" ")
        options = f" {options}" if options else ""
        return f"PRIMARY KEY ({expressions}){options}"

    def if_sql(self, expression: exp.If) -> str:
        return self.case_sql(exp.Case(ifs=[expression], default=expression.args.get("false")))

    def matchagainst_sql(self, expression: exp.MatchAgainst) -> str:
        modifier = expression.args.get("modifier")
        modifier = f" {modifier}" if modifier else ""
        return f"{self.func('MATCH', *expression.expressions)} AGAINST({self.sql(expression, 'this')}{modifier})"

    def jsonkeyvalue_sql(self, expression: exp.JSONKeyValue) -> str:
        return f"{self.sql(expression, 'this')}{self.JSON_KEY_VALUE_PAIR_SEP} {self.sql(expression, 'expression')}"

    def jsonpath_sql(self, expression: exp.JSONPath) -> str:
        path = self.expressions(expression, sep="", flat=True).lstrip(".")
        return f"{self.dialect.QUOTE_START}{path}{self.dialect.QUOTE_END}"

    def json_path_part(self, expression: int | str | exp.JSONPathPart) -> str:
        if isinstance(expression, exp.JSONPathPart):
            transform = self.TRANSFORMS.get(expression.__class__)
            if not callable(transform):
                self.unsupported(f"Unsupported JSONPathPart type {expression.__class__.__name__}")
                return ""

            return transform(self, expression)

        if isinstance(expression, int):
            return str(expression)

        if self.JSON_PATH_SINGLE_QUOTE_ESCAPE:
            escaped = expression.replace("'", "\\'")
            escaped = f"\\'{expression}\\'"
        else:
            escaped = expression.replace('"', '\\"')
            escaped = f'"{escaped}"'

        return escaped

    def formatjson_sql(self, expression: exp.FormatJson) -> str:
        return f"{self.sql(expression, 'this')} FORMAT JSON"

    def jsonobject_sql(self, expression: exp.JSONObject | exp.JSONObjectAgg) -> str:
        null_handling = expression.args.get("null_handling")
        null_handling = f" {null_handling}" if null_handling else ""

        unique_keys = expression.args.get("unique_keys")
        if unique_keys is not None:
            unique_keys = f" {'WITH' if unique_keys else 'WITHOUT'} UNIQUE KEYS"
        else:
            unique_keys = ""

        return_type = self.sql(expression, "return_type")
        return_type = f" RETURNING {return_type}" if return_type else ""
        encoding = self.sql(expression, "encoding")
        encoding = f" ENCODING {encoding}" if encoding else ""

        return self.func(
            "JSON_OBJECT" if isinstance(expression, exp.JSONObject) else "JSON_OBJECTAGG",
            *expression.expressions,
            suffix=f"{null_handling}{unique_keys}{return_type}{encoding})",
        )

    def jsonobjectagg_sql(self, expression: exp.JSONObjectAgg) -> str:
        return self.jsonobject_sql(expression)

    def jsonarray_sql(self, expression: exp.JSONArray) -> str:
        null_handling = expression.args.get("null_handling")
        null_handling = f" {null_handling}" if null_handling else ""
        return_type = self.sql(expression, "return_type")
        return_type = f" RETURNING {return_type}" if return_type else ""
        strict = " STRICT" if expression.args.get("strict") else ""
        return self.func(
            "JSON_ARRAY", *expression.expressions, suffix=f"{null_handling}{return_type}{strict})"
        )

    def jsonarrayagg_sql(self, expression: exp.JSONArrayAgg) -> str:
        this = self.sql(expression, "this")
        order = self.sql(expression, "order")
        null_handling = expression.args.get("null_handling")
        null_handling = f" {null_handling}" if null_handling else ""
        return_type = self.sql(expression, "return_type")
        return_type = f" RETURNING {return_type}" if return_type else ""
        strict = " STRICT" if expression.args.get("strict") else ""
        return self.func(
            "JSON_ARRAYAGG",
            this,
            suffix=f"{order}{null_handling}{return_type}{strict})",
        )

    def jsoncolumndef_sql(self, expression: exp.JSONColumnDef) -> str:
        path = self.sql(expression, "path")
        path = f" PATH {path}" if path else ""
        nested_schema = self.sql(expression, "nested_schema")

        if nested_schema:
            return f"NESTED{path} {nested_schema}"

        this = self.sql(expression, "this")
        kind = self.sql(expression, "kind")
        kind = f" {kind}" if kind else ""
        return f"{this}{kind}{path}"

    def jsonschema_sql(self, expression: exp.JSONSchema) -> str:
        return self.func("COLUMNS", *expression.expressions)

    def jsontable_sql(self, expression: exp.JSONTable) -> str:
        this = self.sql(expression, "this")
        path = self.sql(expression, "path")
        path = f", {path}" if path else ""
        error_handling = expression.args.get("error_handling")
        error_handling = f" {error_handling}" if error_handling else ""
        empty_handling = expression.args.get("empty_handling")
        empty_handling = f" {empty_handling}" if empty_handling else ""
        schema = self.sql(expression, "schema")
        return self.func(
            "JSON_TABLE", this, suffix=f"{path}{error_handling}{empty_handling} {schema})"
        )

    def openjsoncolumndef_sql(self, expression: exp.OpenJSONColumnDef) -> str:
        this = self.sql(expression, "this")
        kind = self.sql(expression, "kind")
        path = self.sql(expression, "path")
        path = f" {path}" if path else ""
        as_json = " AS JSON" if expression.args.get("as_json") else ""
        return f"{this} {kind}{path}{as_json}"

    def openjson_sql(self, expression: exp.OpenJSON) -> str:
        this = self.sql(expression, "this")
        path = self.sql(expression, "path")
        path = f", {path}" if path else ""
        expressions = self.expressions(expression)
        with_ = (
            f" WITH ({self.seg(self.indent(expressions), sep='')}{self.seg(')', sep='')}"
            if expressions
            else ""
        )
        return f"OPENJSON({this}{path}){with_}"

    def in_sql(self, expression: exp.In) -> str:
        query = expression.args.get("query")
        unnest = expression.args.get("unnest")
        field = expression.args.get("field")
        is_global = " GLOBAL" if expression.args.get("is_global") else ""

        if query:
            in_sql = self.wrap(query)
        elif unnest:
            in_sql = self.in_unnest_op(unnest)
        elif field:
            in_sql = self.sql(field)
        else:
            in_sql = f"({self.expressions(expression, flat=True)})"

        return f"{self.sql(expression, 'this')}{is_global} IN {in_sql}"

    def in_unnest_op(self, unnest: exp.Unnest) -> str:
        return f"(SELECT {self.sql(unnest)})"

    def interval_sql(self, expression: exp.Interval) -> str:
        unit = self.sql(expression, "unit")
        if not self.INTERVAL_ALLOWS_PLURAL_FORM:
            unit = self.TIME_PART_SINGULARS.get(unit, unit)
        unit = f" {unit}" if unit else ""

        if self.SINGLE_STRING_INTERVAL:
            this = expression.this.name if expression.this else ""
            return f"INTERVAL '{this}{unit}'" if this else f"INTERVAL{unit}"

        this = self.sql(expression, "this")
        if this:
            unwrapped = isinstance(expression.this, self.UNWRAPPED_INTERVAL_VALUES)
            this = f" {this}" if unwrapped else f" ({this})"

        return f"INTERVAL{this}{unit}"

    def return_sql(self, expression: exp.Return) -> str:
        return f"RETURN {self.sql(expression, 'this')}"

    def reference_sql(self, expression: exp.Reference) -> str:
        this = self.sql(expression, "this")
        expressions = self.expressions(expression, flat=True)
        expressions = f"({expressions})" if expressions else ""
        options = self.expressions(expression, key="options", flat=True, sep=" ")
        options = f" {options}" if options else ""
        return f"REFERENCES {this}{expressions}{options}"

    def anonymous_sql(self, expression: exp.Anonymous) -> str:
        return self.func(expression.name, *expression.expressions)

    def paren_sql(self, expression: exp.Paren) -> str:
        if isinstance(expression.unnest(), exp.Select):
            sql = self.wrap(expression)
        else:
            sql = self.seg(self.indent(self.sql(expression, "this")), sep="")
            sql = f"({sql}{self.seg(')', sep='')}"

        return self.prepend_ctes(expression, sql)

    def neg_sql(self, expression: exp.Neg) -> str:
        # This makes sure we don't convert "- - 5" to "--5", which is a comment
        this_sql = self.sql(expression, "this")
        sep = " " if this_sql[0] == "-" else ""
        return f"-{sep}{this_sql}"

    def not_sql(self, expression: exp.Not) -> str:
        return f"NOT {self.sql(expression, 'this')}"

    def alias_sql(self, expression: exp.Alias) -> str:
        alias = self.sql(expression, "alias")
        alias = f" AS {alias}" if alias else ""
        return f"{self.sql(expression, 'this')}{alias}"

    def pivotalias_sql(self, expression: exp.PivotAlias) -> str:
        alias = expression.args["alias"]
        identifier_alias = isinstance(alias, exp.Identifier)

        if identifier_alias and not self.UNPIVOT_ALIASES_ARE_IDENTIFIERS:
            alias.replace(exp.Literal.string(alias.output_name))
        elif not identifier_alias and self.UNPIVOT_ALIASES_ARE_IDENTIFIERS:
            alias.replace(exp.to_identifier(alias.output_name))

        return self.alias_sql(expression)

    def aliases_sql(self, expression: exp.Aliases) -> str:
        return f"{self.sql(expression, 'this')} AS ({self.expressions(expression, flat=True)})"

    def atindex_sql(self, expression: exp.AtTimeZone) -> str:
        this = self.sql(expression, "this")
        index = self.sql(expression, "expression")
        return f"{this} AT {index}"

    def attimezone_sql(self, expression: exp.AtTimeZone) -> str:
        this = self.sql(expression, "this")
        zone = self.sql(expression, "zone")
        return f"{this} AT TIME ZONE {zone}"

    def fromtimezone_sql(self, expression: exp.FromTimeZone) -> str:
        this = self.sql(expression, "this")
        zone = self.sql(expression, "zone")
        return f"{this} AT TIME ZONE {zone} AT TIME ZONE 'UTC'"

    def add_sql(self, expression: exp.Add) -> str:
        return self.binary(expression, "+")

    def and_sql(self, expression: exp.And) -> str:
        return self.connector_sql(expression, "AND")

    def xor_sql(self, expression: exp.Xor) -> str:
        return self.connector_sql(expression, "XOR")

    def connector_sql(self, expression: exp.Connector, op: str) -> str:
        if not self.pretty:
            return self.binary(expression, op)

        sqls = tuple(
            self.maybe_comment(self.sql(e), e, e.parent.comments or []) if i != 1 else self.sql(e)
            for i, e in enumerate(expression.flatten(unnest=False))
        )

        sep = "\n" if self.text_width(sqls) > self.max_text_width else " "
        return f"{sep}{op} ".join(sqls)

    def bitwiseand_sql(self, expression: exp.BitwiseAnd) -> str:
        return self.binary(expression, "&")

    def bitwiseleftshift_sql(self, expression: exp.BitwiseLeftShift) -> str:
        return self.binary(expression, "<<")

    def bitwisenot_sql(self, expression: exp.BitwiseNot) -> str:
        return f"~{self.sql(expression, 'this')}"

    def bitwiseor_sql(self, expression: exp.BitwiseOr) -> str:
        return self.binary(expression, "|")

    def bitwiserightshift_sql(self, expression: exp.BitwiseRightShift) -> str:
        return self.binary(expression, ">>")

    def bitwisexor_sql(self, expression: exp.BitwiseXor) -> str:
        return self.binary(expression, "^")

    def cast_sql(self, expression: exp.Cast, safe_prefix: t.Optional[str] = None) -> str:
        format_sql = self.sql(expression, "format")
        format_sql = f" FORMAT {format_sql}" if format_sql else ""
        to_sql = self.sql(expression, "to")
        to_sql = f" {to_sql}" if to_sql else ""
        return f"{safe_prefix or ''}TYPE_CONVERSION({self.sql(expression, 'this')})"

    def currentdate_sql(self, expression: exp.CurrentDate) -> str:
        zone = self.sql(expression, "this")
        return f"CURRENT_DATE({zone})" if zone else "CURRENT_DATE"

    def currenttimestamp_sql(self, expression: exp.CurrentTimestamp) -> str:
        return self.func("CURRENT_TIMESTAMP", expression.this)

    def collate_sql(self, expression: exp.Collate) -> str:
        if self.COLLATE_IS_FUNC:
            return self.function_fallback_sql(expression)
        return self.binary(expression, "COLLATE")

    def command_sql(self, expression: exp.Command) -> str:
        return f"{self.sql(expression, 'this')} {expression.text('expression').strip()}"

    def comment_sql(self, expression: exp.Comment) -> str:
        this = self.sql(expression, "this")
        kind = expression.args["kind"]
        exists_sql = " IF EXISTS " if expression.args.get("exists") else " "
        expression_sql = self.sql(expression, "expression")
        return f"COMMENT{exists_sql}ON {kind} {this} IS {expression_sql}"

    def mergetreettlaction_sql(self, expression: exp.MergeTreeTTLAction) -> str:
        this = self.sql(expression, "this")
        delete = " DELETE" if expression.args.get("delete") else ""
        recompress = self.sql(expression, "recompress")
        recompress = f" RECOMPRESS {recompress}" if recompress else ""
        to_disk = self.sql(expression, "to_disk")
        to_disk = f" TO DISK {to_disk}" if to_disk else ""
        to_volume = self.sql(expression, "to_volume")
        to_volume = f" TO VOLUME {to_volume}" if to_volume else ""
        return f"{this}{delete}{recompress}{to_disk}{to_volume}"

    def mergetreettl_sql(self, expression: exp.MergeTreeTTL) -> str:
        where = self.sql(expression, "where")
        group = self.sql(expression, "group")
        aggregates = self.expressions(expression, key="aggregates")
        aggregates = self.seg("SET") + self.seg(aggregates) if aggregates else ""

        if not (where or group or aggregates) and len(expression.expressions) == 1:
            return f"TTL {self.expressions(expression, flat=True)}"

        return f"TTL{self.seg(self.expressions(expression))}{where}{group}{aggregates}"

    def transaction_sql(self, expression: exp.Transaction) -> str:
        return "BEGIN"

    def commit_sql(self, expression: exp.Commit) -> str:
        chain = expression.args.get("chain")
        if chain is not None:
            chain = " AND CHAIN" if chain else " AND NO CHAIN"

        return f"COMMIT{chain or ''}"

    def rollback_sql(self, expression: exp.Rollback) -> str:
        savepoint = expression.args.get("savepoint")
        savepoint = f" TO {savepoint}" if savepoint else ""
        return f"ROLLBACK{savepoint}"

    def altercolumn_sql(self, expression: exp.AlterColumn) -> str:
        this = self.sql(expression, "this")

        dtype = self.sql(expression, "dtype")
        if dtype:
            collate = self.sql(expression, "collate")
            collate = f" COLLATE {collate}" if collate else ""
            using = self.sql(expression, "using")
            using = f" USING {using}" if using else ""
            return f"ALTER COLUMN {this} SET DATA TYPE {dtype}{collate}{using}"

        default = self.sql(expression, "default")
        if default:
            return f"ALTER COLUMN {this} SET DEFAULT {default}"

        comment = self.sql(expression, "comment")
        if comment:
            return f"ALTER COLUMN {this} COMMENT {comment}"

        if not expression.args.get("drop"):
            self.unsupported("Unsupported ALTER COLUMN syntax")

        return f"ALTER COLUMN {this} DROP DEFAULT"

    def renametable_sql(self, expression: exp.RenameTable) -> str:
        if not self.RENAME_TABLE_WITH_DB:
            # Remove db from tables
            expression = expression.transform(
                lambda n: exp.table_(n.this) if isinstance(n, exp.Table) else n
            )
        this = self.sql(expression, "this")
        return f"RENAME TO {this}"

    def renamecolumn_sql(self, expression: exp.RenameColumn) -> str:
        exists = " IF EXISTS" if expression.args.get("exists") else ""
        old_column = self.sql(expression, "this")
        new_column = self.sql(expression, "to")
        return f"RENAME COLUMN{exists} {old_column} TO {new_column}"

    def altertable_sql(self, expression: exp.AlterTable) -> str:
        actions = expression.args["actions"]

        if isinstance(actions[0], exp.ColumnDef):
            actions = self.add_column_sql(expression)
        elif isinstance(actions[0], exp.Schema):
            actions = self.expressions(expression, key="actions", prefix="ADD COLUMNS ")
        elif isinstance(actions[0], exp.Delete):
            actions = self.expressions(expression, key="actions", flat=True)
        else:
            actions = self.expressions(expression, key="actions", flat=True)

        exists = " IF EXISTS" if expression.args.get("exists") else ""
        only = " ONLY" if expression.args.get("only") else ""
        return f"ALTER TABLE{exists}{only} {self.sql(expression, 'this')} {actions}"

    def add_column_sql(self, expression: exp.AlterTable) -> str:
        if self.ALTER_TABLE_INCLUDE_COLUMN_KEYWORD:
            return self.expressions(
                expression,
                key="actions",
                prefix="ADD COLUMN ",
            )
        return f"ADD {self.expressions(expression, key='actions', flat=True)}"

    def droppartition_sql(self, expression: exp.DropPartition) -> str:
        expressions = self.expressions(expression)
        exists = " IF EXISTS " if expression.args.get("exists") else " "
        return f"DROP{exists}{expressions}"

    def addconstraint_sql(self, expression: exp.AddConstraint) -> str:
        this = self.sql(expression, "this")
        expression_ = self.sql(expression, "expression")
        add_constraint = f"ADD CONSTRAINT {this}" if this else "ADD"

        enforced = expression.args.get("enforced")
        if enforced is not None:
            return f"{add_constraint} CHECK ({expression_}){' ENFORCED' if enforced else ''}"

        return f"{add_constraint} {expression_}"

    def distinct_sql(self, expression: exp.Distinct) -> str:
        this = self.expressions(expression, flat=True)

        if not self.MULTI_ARG_DISTINCT and len(expression.expressions) > 1:
            case = exp.case()
            for arg in expression.expressions:
                case = case.when(arg.is_(exp.null()), exp.null())
            this = self.sql(case.else_(f"({this})"))

        this = f" {this}" if this else ""

        on = self.sql(expression, "on")
        on = f" ON {on}" if on else ""
        return f"DISTINCT{this}{on}"

    def ignorenulls_sql(self, expression: exp.IgnoreNulls) -> str:
        return self._embed_ignore_nulls(expression, "IGNORE NULLS")

    def respectnulls_sql(self, expression: exp.RespectNulls) -> str:
        return self._embed_ignore_nulls(expression, "RESPECT NULLS")

    def havingmax_sql(self, expression: exp.HavingMax) -> str:
        this_sql = self.sql(expression, "this")
        expression_sql = self.sql(expression, "expression")
        kind = "MAX" if expression.args.get("max") else "MIN"
        return f"{this_sql} HAVING {kind} {expression_sql}"

    def _embed_ignore_nulls(self, expression: exp.IgnoreNulls | exp.RespectNulls, text: str) -> str:
        if self.IGNORE_NULLS_IN_FUNC and not expression.meta.get("inline"):
            # The first modifier here will be the one closest to the AggFunc's arg
            mods = sorted(
                expression.find_all(exp.HavingMax, exp.Order, exp.Limit),
                key=lambda x: 0
                if isinstance(x, exp.HavingMax)
                else (1 if isinstance(x, exp.Order) else 2),
            )

            if mods:
                mod = mods[0]
                this = expression.__class__(this=mod.this.copy())
                this.meta["inline"] = True
                mod.this.replace(this)
                return self.sql(expression.this)

            agg_func = expression.find(exp.AggFunc)

            if agg_func:
                return self.sql(agg_func)[:-1] + f" {text})"

        return f"{self.sql(expression, 'this')} {text}"

    def intdiv_sql(self, expression: exp.IntDiv) -> str:
        return self.sql(
            exp.Cast(
                this=exp.Div(this=expression.this, expression=expression.expression),
                to=exp.DataType(this=exp.DataType.Type.INT),
            )
        )

    def dpipe_sql(self, expression: exp.DPipe) -> str:
        if self.dialect.STRICT_STRING_CONCAT and expression.args.get("safe"):
            return self.func("CONCAT", *(exp.cast(e, "text") for e in expression.flatten()))
        return self.binary(expression, "||")

    def div_sql(self, expression: exp.Div) -> str:
        l, r = expression.left, expression.right

        if not self.dialect.SAFE_DIVISION and expression.args.get("safe"):
            r.replace(exp.Nullif(this=r.copy(), expression=exp.Literal.number(0)))

        if self.dialect.TYPED_DIVISION and not expression.args.get("typed"):
            if not l.is_type(*exp.DataType.FLOAT_TYPES) and not r.is_type(
                *exp.DataType.FLOAT_TYPES
            ):
                l.replace(exp.cast(l.copy(), to=exp.DataType.Type.DOUBLE))

        elif not self.dialect.TYPED_DIVISION and expression.args.get("typed"):
            if l.is_type(*exp.DataType.INTEGER_TYPES) and r.is_type(*exp.DataType.INTEGER_TYPES):
                return self.sql(
                    exp.cast(
                        l / r,
                        to=exp.DataType.Type.BIGINT,
                    )
                )

        return self.binary(expression, "/")

    def overlaps_sql(self, expression: exp.Overlaps) -> str:
        return self.binary(expression, "OVERLAPS")

    def distance_sql(self, expression: exp.Distance) -> str:
        return self.binary(expression, "<->")

    def dot_sql(self, expression: exp.Dot) -> str:
        return f"{self.sql(expression, 'this')}.{self.sql(expression, 'expression')}"

    def eq_sql(self, expression: exp.EQ) -> str:
        return self.binary(expression, "=")

    def propertyeq_sql(self, expression: exp.PropertyEQ) -> str:
        return self.binary(expression, ":=")

    def escape_sql(self, expression: exp.Escape) -> str:
        return self.binary(expression, "ESCAPE")

    def glob_sql(self, expression: exp.Glob) -> str:
        return self.binary(expression, "GLOB")

    def gt_sql(self, expression: exp.GT) -> str:
        return self.binary(expression, ">")

    def gte_sql(self, expression: exp.GTE) -> str:
        return self.binary(expression, ">=")

    def ilike_sql(self, expression: exp.ILike) -> str:
        return self.binary(expression, "ILIKE")

    def ilikeany_sql(self, expression: exp.ILikeAny) -> str:
        return self.binary(expression, "ILIKE ANY")

    def is_sql(self, expression: exp.Is) -> str:
        if not self.IS_BOOL_ALLOWED and isinstance(expression.expression, exp.Boolean):
            return self.sql(
                expression.this if expression.expression.this else exp.not_(expression.this)
            )
        return self.binary(expression, "IS")

    def like_sql(self, expression: exp.Like) -> str:
        return self.binary(expression, "LIKE")

    def likeany_sql(self, expression: exp.LikeAny) -> str:
        return self.binary(expression, "LIKE ANY")

    def similarto_sql(self, expression: exp.SimilarTo) -> str:
        return self.binary(expression, "SIMILAR TO")

    def lt_sql(self, expression: exp.LT) -> str:
        return self.binary(expression, "<")

    def lte_sql(self, expression: exp.LTE) -> str:
        return self.binary(expression, "<=")

    def mod_sql(self, expression: exp.Mod) -> str:
        return self.binary(expression, "%")

    def mul_sql(self, expression: exp.Mul) -> str:
        return self.binary(expression, "**")

    def neq_sql(self, expression: exp.NEQ) -> str:
        return self.binary(expression, "<>")

    def nullsafeeq_sql(self, expression: exp.NullSafeEQ) -> str:
        return self.binary(expression, "IS NOT DISTINCT FROM")

    def nullsafeneq_sql(self, expression: exp.NullSafeNEQ) -> str:
        return self.binary(expression, "IS DISTINCT FROM")

    def or_sql(self, expression: exp.Or) -> str:
        return self.connector_sql(expression, "OR")

    def slice_sql(self, expression: exp.Slice) -> str:
        return self.binary(expression, ":")

    def sub_sql(self, expression: exp.Sub) -> str:
        return self.binary(expression, "-")

    def trycast_sql(self, expression: exp.TryCast) -> str:
        return self.cast_sql(expression, safe_prefix="TRY_")

    def log_sql(self, expression: exp.Log) -> str:
        this = expression.this
        expr = expression.expression

        if not self.dialect.LOG_BASE_FIRST:
            this, expr = expr, this

        return self.func("LOG", this, expr)

    def use_sql(self, expression: exp.Use) -> str:
        kind = self.sql(expression, "kind")
        kind = f" {kind}" if kind else ""
        this = self.sql(expression, "this")
        this = f" {this}" if this else ""
        return f"USE{kind}{this}"

    def binary(self, expression: exp.Binary, op: str) -> str:
        op = self.maybe_comment(op, comments=expression.comments)
        return f"{self.sql(expression, 'this')} {op} {self.sql(expression, 'expression')}"

    def function_fallback_sql(self, expression: exp.Func) -> str:
        args = []

        for key in expression.arg_types:
            arg_value = expression.args.get(key)

            if isinstance(arg_value, list):
                for value in arg_value:
                    args.append(value)
            elif arg_value is not None:
                args.append(arg_value)

        if self.normalize_functions:
            name = expression.sql_name()
        else:
            name = (expression._meta and expression.meta.get("name")) or expression.sql_name()

        return self.func(name, *args)

    def func(
        self,
        name: str,
        *args: t.Optional[exp.Expression | str],
        prefix: str = "(",
        suffix: str = ")",
    ) -> str:
        return f"{self.normalize_func(name)}{prefix}{self.format_args(*args)}{suffix}"

    def format_args(self, *args: t.Optional[str | exp.Expression]) -> str:
        arg_sqls = tuple(self.sql(arg) for arg in args if arg is not None)
        if self.pretty and self.text_width(arg_sqls) > self.max_text_width:
            return self.indent("\n" + ",\n".join(arg_sqls) + "\n", skip_first=True, skip_last=True)
        return ", ".join(arg_sqls)

    def text_width(self, args: t.Iterable) -> int:
        return sum(len(arg) for arg in args)

    def format_time(self, expression: exp.Expression) -> t.Optional[str]:
        return format_time(
            self.sql(expression, "format"),
            self.dialect.INVERSE_TIME_MAPPING,
            self.dialect.INVERSE_TIME_TRIE,
        )

    def expressions(
        self,
        expression: t.Optional[exp.Expression] = None,
        key: t.Optional[str] = None,
        sqls: t.Optional[t.Collection[str | exp.Expression]] = None,
        flat: bool = False,
        indent: bool = True,
        skip_first: bool = False,
        sep: str = ", ",
        prefix: str = "",
    ) -> str:
        expressions = expression.args.get(key or "expressions") if expression else sqls

        if not expressions:
            return ""

        if flat:
            return sep.join(sql for sql in (self.sql(e) for e in expressions) if sql)

        num_sqls = len(expressions)

        # These are calculated once in case we have the leading_comma / pretty option set, correspondingly
        pad = " " * self.pad
        stripped_sep = sep.strip()

        result_sqls = []
        for i, e in enumerate(expressions):
            sql = self.sql(e, comment=False)
            if not sql:
                continue

            comments = self.maybe_comment("", e) if isinstance(e, exp.Expression) else ""

            if self.pretty:
                if self.leading_comma:
                    result_sqls.append(f"{sep if i > 0 else pad}{prefix}{sql}{comments}")
                else:
                    result_sqls.append(
                        f"{prefix}{sql}{stripped_sep if i + 1 < num_sqls else ''}{comments}"
                    )
            else:
                result_sqls.append(f"{prefix}{sql}{comments}{sep if i + 1 < num_sqls else ''}")

        result_sql = "\n".join(result_sqls) if self.pretty else "".join(result_sqls)
        return self.indent(result_sql, skip_first=skip_first) if indent else result_sql

    def op_expressions(self, op: str, expression: exp.Expression, flat: bool = False) -> str:
        flat = flat or isinstance(expression.parent, exp.Properties)
        expressions_sql = self.expressions(expression, flat=flat)
        if flat:
            return f"{op} {expressions_sql}"
        return f"{self.seg(op)}{self.sep() if expressions_sql else ''}{expressions_sql}"

    def naked_property(self, expression: exp.Property) -> str:
        property_name = exp.Properties.PROPERTY_TO_NAME.get(expression.__class__)
        if not property_name:
            self.unsupported(f"Unsupported property {expression.__class__.__name__}")
        return f"{property_name} {self.sql(expression, 'this')}"

    def set_operation(self, expression: exp.Union, op: str) -> str:
        this = self.maybe_comment(self.sql(expression, "this"), comments=expression.comments)
        op = self.seg(op)
        return self.query_modifiers(
            expression, f"{this}{op}{self.sep()}{self.sql(expression, 'expression')}"
        )

    def tag_sql(self, expression: exp.Tag) -> str:
        return f"{expression.args.get('prefix')}{self.sql(expression.this)}{expression.args.get('postfix')}"

    def token_sql(self, token_type: TokenType) -> str:
        return self.TOKEN_MAPPING.get(token_type, token_type.name)

    def userdefinedfunction_sql(self, expression: exp.UserDefinedFunction) -> str:
        this = self.sql(expression, "this")
        expressions = self.no_identify(self.expressions, expression)
        expressions = (
            self.wrap(expressions) if expression.args.get("wrapped") else f" {expressions}"
        )
        return f"{this}{expressions}"

    def joinhint_sql(self, expression: exp.JoinHint) -> str:
        this = self.sql(expression, "this")
        expressions = self.expressions(expression, flat=True)
        return f"{this}({expressions})"

    def kwarg_sql(self, expression: exp.Kwarg) -> str:
        return self.binary(expression, "=>")

    def when_sql(self, expression: exp.When) -> str:
        matched = "MATCHED" if expression.args["matched"] else "NOT MATCHED"
        source = " BY SOURCE" if self.MATCHED_BY_SOURCE and expression.args.get("source") else ""
        condition = self.sql(expression, "condition")
        condition = f" AND {condition}" if condition else ""

        then_expression = expression.args.get("then")
        if isinstance(then_expression, exp.Insert):
            then = f"INSERT {self.sql(then_expression, 'this')}"
            if "expression" in then_expression.args:
                then += f" VALUES {self.sql(then_expression, 'expression')}"
        elif isinstance(then_expression, exp.Update):
            if isinstance(then_expression.args.get("expressions"), exp.Star):
                then = f"UPDATE {self.sql(then_expression, 'expressions')}"
            else:
                then = f"UPDATE SET {self.expressions(then_expression, flat=True)}"
        else:
            then = self.sql(then_expression)
        return f"WHEN {matched}{source}{condition} THEN {then}"

    def merge_sql(self, expression: exp.Merge) -> str:
        table = expression.this
        table_alias = ""

        hints = table.args.get("hints")
        if hints and table.alias and isinstance(hints[0], exp.WithTableHint):
            # T-SQL syntax is MERGE ... <target_table> [WITH (<merge_hint>)] [[AS] table_alias]
            table_alias = f" AS {self.sql(table.args['alias'].pop())}"

        this = self.sql(table)
        using = f"USING {self.sql(expression, 'using')}"
        on = f"ON {self.sql(expression, 'on')}"
        expressions = self.expressions(expression, sep=" ")

        return self.prepend_ctes(
            expression, f"MERGE INTO {this}{table_alias} {using} {on} {expressions}"
        )

    def tochar_sql(self, expression: exp.ToChar) -> str:
        if expression.args.get("format"):
            self.unsupported("Format argument unsupported for TO_CHAR/TO_VARCHAR function")

        return self.sql(exp.cast(expression.this, "text"))

    def dictproperty_sql(self, expression: exp.DictProperty) -> str:
        this = self.sql(expression, "this")
        kind = self.sql(expression, "kind")
        settings_sql = self.expressions(expression, key="settings", sep=" ")
        args = f"({self.sep('')}{settings_sql}{self.seg(')', sep='')}" if settings_sql else "()"
        return f"{this}({kind}{args})"

    def dictrange_sql(self, expression: exp.DictRange) -> str:
        this = self.sql(expression, "this")
        max = self.sql(expression, "max")
        min = self.sql(expression, "min")
        return f"{this}(MIN {min} MAX {max})"

    def dictsubproperty_sql(self, expression: exp.DictSubProperty) -> str:
        return f"{self.sql(expression, 'this')} {self.sql(expression, 'value')}"

    def oncluster_sql(self, expression: exp.OnCluster) -> str:
        return ""

    def clusteredbyproperty_sql(self, expression: exp.ClusteredByProperty) -> str:
        expressions = self.expressions(expression, key="expressions", flat=True)
        sorted_by = self.expressions(expression, key="sorted_by", flat=True)
        sorted_by = f" SORTED BY ({sorted_by})" if sorted_by else ""
        buckets = self.sql(expression, "buckets")
        return f"CLUSTERED BY ({expressions}){sorted_by} INTO {buckets} BUCKETS"

    def anyvalue_sql(self, expression: exp.AnyValue) -> str:
        this = self.sql(expression, "this")
        having = self.sql(expression, "having")

        if having:
            this = f"{this} HAVING {'MAX' if expression.args.get('max') else 'MIN'} {having}"

        return self.func("ANY_VALUE", this)

    def querytransform_sql(self, expression: exp.QueryTransform) -> str:
        transform = self.func("TRANSFORM", *expression.expressions)
        row_format_before = self.sql(expression, "row_format_before")
        row_format_before = f" {row_format_before}" if row_format_before else ""
        record_writer = self.sql(expression, "record_writer")
        record_writer = f" RECORDWRITER {record_writer}" if record_writer else ""
        using = f" USING {self.sql(expression, 'command_script')}"
        schema = self.sql(expression, "schema")
        schema = f" AS {schema}" if schema else ""
        row_format_after = self.sql(expression, "row_format_after")
        row_format_after = f" {row_format_after}" if row_format_after else ""
        record_reader = self.sql(expression, "record_reader")
        record_reader = f" RECORDREADER {record_reader}" if record_reader else ""
        return f"{transform}{row_format_before}{record_writer}{using}{schema}{row_format_after}{record_reader}"

    def indexconstraintoption_sql(self, expression: exp.IndexConstraintOption) -> str:
        key_block_size = self.sql(expression, "key_block_size")
        if key_block_size:
            return f"KEY_BLOCK_SIZE = {key_block_size}"

        using = self.sql(expression, "using")
        if using:
            return f"USING {using}"

        parser = self.sql(expression, "parser")
        if parser:
            return f"WITH PARSER {parser}"

        comment = self.sql(expression, "comment")
        if comment:
            return f"COMMENT {comment}"

        visible = expression.args.get("visible")
        if visible is not None:
            return "VISIBLE" if visible else "INVISIBLE"

        engine_attr = self.sql(expression, "engine_attr")
        if engine_attr:
            return f"ENGINE_ATTRIBUTE = {engine_attr}"

        secondary_engine_attr = self.sql(expression, "secondary_engine_attr")
        if secondary_engine_attr:
            return f"SECONDARY_ENGINE_ATTRIBUTE = {secondary_engine_attr}"

        self.unsupported("Unsupported index constraint option.")
        return ""

    def indexcolumnconstraint_sql(self, expression: exp.IndexColumnConstraint) -> str:
        kind = self.sql(expression, "kind")
        kind = f"{kind} INDEX" if kind else "INDEX"
        this = self.sql(expression, "this")
        this = f" {this}" if this else ""
        index_type = self.sql(expression, "index_type")
        index_type = f" USING {index_type}" if index_type else ""
        schema = self.sql(expression, "schema")
        schema = f" {schema}" if schema else ""
        options = self.expressions(expression, key="options", sep=" ")
        options = f" {options}" if options else ""
        return f"{kind}{this}{index_type}{schema}{options}"

    def nvl2_sql(self, expression: exp.Nvl2) -> str:
        if self.NVL2_SUPPORTED:
            return self.function_fallback_sql(expression)

        case = exp.Case().when(
            expression.this.is_(exp.null()).not_(copy=False),
            expression.args["true"],
            copy=False,
        )
        else_cond = expression.args.get("false")
        if else_cond:
            case.else_(else_cond, copy=False)

        return self.sql(case)

    def comprehension_sql(self, expression: exp.Comprehension) -> str:
        this = self.sql(expression, "this")
        expr = self.sql(expression, "expression")
        iterator = self.sql(expression, "iterator")
        condition = self.sql(expression, "condition")
        condition = f" IF {condition}" if condition else ""
        return f"{this} FOR {expr} IN {iterator}{condition}"

    def columnprefix_sql(self, expression: exp.ColumnPrefix) -> str:
        return f"{self.sql(expression, 'this')}({self.sql(expression, 'expression')})"

    def opclass_sql(self, expression: exp.Opclass) -> str:
        return f"{self.sql(expression, 'this')} {self.sql(expression, 'expression')}"

    def predict_sql(self, expression: exp.Predict) -> str:
        model = self.sql(expression, "this")
        model = f"MODEL {model}"
        table = self.sql(expression, "expression")
        table = f"TABLE {table}" if not isinstance(expression.expression, exp.Subquery) else table
        parameters = self.sql(expression, "params_struct")
        return self.func("PREDICT", model, table, parameters or None)

    def forin_sql(self, expression: exp.ForIn) -> str:
        this = self.sql(expression, "this")
        expression_sql = self.sql(expression, "expression")
        return f"FOR {this} DO {expression_sql}"

    def refresh_sql(self, expression: exp.Refresh) -> str:
        this = self.sql(expression, "this")
        table = "" if isinstance(expression.this, exp.Literal) else "TABLE "
        return f"REFRESH {table}{this}"

    def operator_sql(self, expression: exp.Operator) -> str:
        return self.binary(expression, f"OPERATOR({self.sql(expression, 'operator')})")

    def toarray_sql(self, expression: exp.ToArray) -> str:
        arg = expression.this
        if not arg.type:
            from sqlglot.optimizer.annotate_types import annotate_types

            arg = annotate_types(arg)

        if arg.is_type(exp.DataType.Type.ARRAY):
            return self.sql(arg)

        cond_for_null = arg.is_(exp.null())
        return self.sql(exp.func("IF", cond_for_null, exp.null(), exp.array(arg, copy=False)))

    def tsordstotime_sql(self, expression: exp.TsOrDsToTime) -> str:
        this = expression.this
        if isinstance(this, exp.TsOrDsToTime) or this.is_type(exp.DataType.Type.TIME):
            return self.sql(this)

        return self.sql(exp.cast(this, "time"))

    def tsordstodate_sql(self, expression: exp.TsOrDsToDate) -> str:
        this = expression.this
        time_format = self.format_time(expression)

        if time_format and time_format not in (self.dialect.TIME_FORMAT, self.dialect.DATE_FORMAT):
            return self.sql(
                exp.cast(exp.StrToTime(this=this, format=expression.args["format"]), "date")
            )

        if isinstance(this, exp.TsOrDsToDate) or this.is_type(exp.DataType.Type.DATE):
            return self.sql(this)

        return self.sql(exp.cast(this, "date"))

    def unixdate_sql(self, expression: exp.UnixDate) -> str:
        return self.sql(
            exp.func(
                "DATEDIFF",
                expression.this,
                exp.cast(exp.Literal.string("1970-01-01"), "date"),
                "day",
            )
        )

    def lastday_sql(self, expression: exp.LastDay) -> str:
        if self.LAST_DAY_SUPPORTS_DATE_PART:
            return self.function_fallback_sql(expression)

        unit = expression.text("unit")
        if unit and unit != "MONTH":
            self.unsupported("Date parts are not supported in LAST_DAY.")

        return self.func("LAST_DAY", expression.this)

    def _jsonpathkey_sql(self, expression: exp.JSONPathKey) -> str:
        this = expression.this
        if isinstance(this, exp.JSONPathWildcard):
            this = self.json_path_part(this)
            return f".{this}" if this else ""

        if exp.SAFE_IDENTIFIER_RE.match(this):
            return f".{this}"

        this = self.json_path_part(this)
        return f"[{this}]" if self.JSON_PATH_BRACKETED_KEY_SUPPORTED else f".{this}"

    def _jsonpathsubscript_sql(self, expression: exp.JSONPathSubscript) -> str:
        this = self.json_path_part(expression.this)
        return f"[{this}]" if this else ""

    def _simplify_unless_literal(self, expression: E) -> E:
        if not isinstance(expression, exp.Literal):
            from sqlglot.optimizer.simplify import simplify

            expression = simplify(expression, dialect=self.dialect)

        return expression

    def _ensure_string_if_null(self, values: t.List[exp.Expression]) -> t.List[exp.Expression]:
        return [
            exp.func("COALESCE", exp.cast(value, "text"), exp.Literal.string(""))
            for value in values
            if value
        ]
