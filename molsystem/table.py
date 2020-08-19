# -*- coding: utf-8 -*-

import collections.abc
import logging
import pandas
from typing import Any, Dict

logger = logging.getLogger(__name__)


class _Table(collections.abc.MutableMapping):
    """A dictionary-like object for holding tabular data

    This is a wrapper around and SQL table, with columns being the attributes.

    Attributes can be created, either from a list of predefined
    ones or by specifying the metadata required of an attribute. Attributes can
    also be removed. See the method 'add_attribute' for more detail.

    Rows can be added ('append') or removed ('delete').
    """

    def __init__(self, system, table: str) -> None:
        self._system = system
        self._table = table

    def __enter__(self) -> Any:
        self.system.__enter__()
        return self

    def __exit__(self, etype, value, traceback) -> None:
        self.system.__exit__(etype, value, traceback)

    def __getitem__(self, key) -> Any:
        """Allow [] to access the data!"""
        result = []
        for line in self.cursor.execute(f'SELECT {key} FROM {self.table}'):
            result.append(line[0])
        return result

    def __setitem__(self, key, value) -> None:
        """Allow x[key] access to the data"""
        length = self.length_of_values(value)
        if length == 1:
            self.cursor.execute(f'UPDATE {self.table} SET {key} = ?', value)
        elif length == self.n_rows:
            parameters = []
            self.cursor.execute(f'SELECT rowid FROM {self.table}')
            for val in value:
                rowid = self.cursor.fetchone()[0]
                parameters.append((val, rowid))

            self.cursor.executemany(
                f'UPDATE {self.table} SET {key} = ? WHERE rowid = ?',
                parameters
            )
        elif length == 0:
            self.cursor.execute(f'UPDATE {self.table} SET {key} = ?', (value,))
        else:
            raise ValueError(
                f'The number of values ({length}) must be 1 or the number of '
                f'rows ({self.n_rows})'
            )

    def __delitem__(self, key) -> None:
        """Allow deletion of keys"""
        # The easy way, which is not supported in SQLite :-(
        # self.cursor.execute(f'ALTER TABLE {self.table} DROP {key}')

        columns = set(self.attributes)
        columns.remove(key)
        column_def = ', '.join(columns)

        sql = f"""
        -- disable foreign key constraint check
        PRAGMA foreign_keys=off;

        -- Here you can drop column
        CREATE TABLE tmp_{self.table}
           AS SELECT {column_def} FROM {self.table};

        -- drop the table
        DROP TABLE {self.table};

        -- rename the new_table to the table
        ALTER TABLE tmp_{self.table} RENAME TO {self.table};

        -- enable foreign key constraint check
        PRAGMA foreign_keys=on;
        """

        with self.db:
            self.db.executescript(sql)

    def __iter__(self) -> Any:
        """Allow iteration over the object"""
        return iter(self.attributes.keys())

    def __len__(self) -> int:
        """The len() command"""
        return len(self.attributes)

    def __repr__(self) -> str:
        """The string representation of this object"""
        df = self.to_dataframe()
        return repr(df)

    def __str__(self) -> str:
        """The pretty string representation of this object"""
        df = self.to_dataframe()
        return str(df)

    def __contains__(self, item) -> bool:
        """Return a boolean indicating if a key exists."""
        return item in self.attributes

    def __eq__(self, other) -> Any:
        """Return a boolean if this object is equal to another"""
        # Check numbers of rows
        if self.n_rows != other.n_rows:
            return False

        # Check the columns
        attributes = self.attributes
        columns = set(attributes)

        other_attributes = other.attributes
        other_columns = set(other_attributes)

        if columns != other_columns:
            return False

        is_same = True
        # Need to check the contents of the tables. See if they are in the same
        # database or if we need to attach the other database temporarily.
        filename = self.system.filename
        other_filename = other.system.filename
        if filename != other_filename:
            # Attach the previous database in order to do comparisons.
            self.cursor.execute(f"ATTACH DATABASE '{other_filename}' AS other")
            other_table = f'other.{other.table}'
        else:
            other_table = other.table
        table = self.table
        self.cursor.execute(
            f"""
            SELECT COUNT(*) FROM
            (SELECT rowid, * FROM {other_table}
            EXCEPT
            SELECT rowid, * FROM {table})
            """
        )
        if self.cursor.fetchone()[0] != 0:
            is_same = False
        else:
            self.cursor.execute(
                f"""
                SELECT COUNT(*) FROM
                (SELECT rowid, * FROM {table}
                EXCEPT
                SELECT rowid, * FROM {other_table})
                """
            )
            if self.cursor.fetchone()[0] != 0:
                is_same = False

        # Detach the other database if needed
        if filename != other_filename:
            self.cursor.execute("DETACH DATABASE other")

        return is_same

    @property
    def system(self):
        """The system that we belong to."""
        return self._system

    @property
    def db(self):
        """The database connection."""
        return self.system.db

    @property
    def cursor(self):
        """The database connection."""
        return self.system.cursor

    @property
    def table(self) -> str:
        """The name of this table."""
        return self._table

    @property
    def n_rows(self) -> int:
        """The number of rows in the table."""
        self.cursor.execute(f'SELECT COUNT(*) FROM {self.table}')
        result = self.cursor.fetchone()[0]
        return result

    @property
    def attributes(self) -> Dict[str, Any]:
        """The definitions of the attributes."""
        result = {}
        for line in self.db.execute(
            "   SELECT *"
            f"    FROM pragma_table_info('{self.table}')"
            " ORDER BY 'cid'"
        ):
            result[line['name']] = {
                'type': line['type'],
                'notnull': bool(line['notnull']),
                'default': line['dflt_value'],
                'primary key': bool(line['pk'])
            }
        return result

    @property
    def version(self):
        return self.system.version

    def add_attribute(
        self,
        name: str,
        coltype: str = 'float',
        default: Any = None,
        notnull: bool = False,
        index: bool = False,
        pk: bool = False,
        references: str = None,
        values: Any = None
    ) -> None:
        """Adds a new attribute.

        If the attribute has been defined previously, the column type and
        default will automatically be those given in the definition. If they
        are provided as arguments, they must be the same as in the definition.

        If the default value is None, you must always provide values wherever
        needed, for example when adding a row.

        Args:
            name: the name of the attribute.
            coltype: the type of the attribute (column). Must be one of 'int',
                'float', 'str' or 'byte'
            default: the default value for the attribute if no value is given.
            notnull: whether the value must be non-null
            values: either a single value or a list of values length 'nrows' of
                values to fill the column.

        Returns:
            None
        """

        # Does the table exist?
        if self.table in self.system:
            table_exists = True
            if pk:
                raise ValueError(
                    'The primary key can only be set on the first attribute '
                    'of a table that does not yet exist.'
                )
        else:
            table_exists = False
            if pk:
                notnull = True

        if name in self:
            raise RuntimeError(
                "{} attribute '{}' is already defined!".format(
                    self.__class__.__name__, name
                )
            )

        # not null columns must have defaults
        if not pk and notnull and default is None:
            raise ValueError(f'Not null attributes must have defaults: {name}')

        # see if the values are given
        if values is not None:
            length = self.length_of_values(values)

            if length > 1 and length != self.n_rows:
                raise IndexError(
                    f"The number of values given, {length}, must be either 1, "
                    f"or the number of rows in {self.table}: {self.n_rows}"
                )

        # Create the column
        parameters = []
        column_def = f'{name} {coltype}'
        if default is not None:
            if coltype == 'str':
                column_def += f" DEFAULT '{default}'"
            else:
                column_def += f' DEFAULT {default}'
        if notnull:
            column_def += ' NOT NULL'
        if references is not None:
            column_def += f' REFERENCES {references}'

        if table_exists:
            if pk:
                column_def += ' PRIMARY KEY'
            self.cursor.execute(
                f'ALTER TABLE {self.table} ADD {column_def}', parameters
            )
        else:
            self.cursor.execute(
                f'CREATE TABLE {self.table} ({column_def})', parameters
            )

        if index == 'unique':
            self.cursor.execute(
                f"CREATE UNIQUE INDEX idx_{name} ON {self.table} ('{name}')"
            )
        elif index:
            self.cursor.execute(
                f"CREATE INDEX idx_{name} ON {self.table} ('{name}')"
            )

        if values is not None:
            self[name] = values

    def append(self, **kwargs: Dict[str, Any]) -> None:
        """Append one or more rows

        The keywords are the names of attributes and the value to use.
        The default value for any attributes not given is used unless it is
        'None' in which case an error is thrown. It is an error if there is not
        an exisiting attribute corresponding to any given as arguments.

        Args:
            kwargs: any number <attribute name> = <value> keyword arguments
                giving existing attributes and values.

        Returns:
            None
        """

        # Check keys and lengths of added rows
        n_rows = None

        lengths = {}
        for key, value in kwargs.items():
            if key not in self:
                raise KeyError(
                    f'"{key}" is not an attribute of the table '
                    f'"{self.table}"!'
                )
            length = self.length_of_values(value)
            lengths[key] = length

            if n_rows is None:
                n_rows = 1 if length == 0 else length

            if length > 1 and length != n_rows:
                if n_rows == 1:
                    n_rows = length
                else:
                    raise IndexError(
                        'key "{}" has the wrong number of values, '
                        .format(key) +
                        '{}. Should be 1 or the number of rows in {} ({}).'
                        .format(length, self.table, n_rows)
                    )

        # Check that any missing attributes have defaults
        attributes = self.attributes
        for key in attributes:
            if key not in kwargs:
                if (
                    'default' not in attributes[key] or
                    attributes[key]['default'] is None
                ):
                    raise KeyError(
                        "There is no default for attribute "
                        "'{}'. You must supply a value".format(key)
                    )

        # All okay, so proceed.
        values = {}
        for key, value in kwargs.items():
            if lengths[key] == 0:
                values[key] = [value] * n_rows
            elif lengths[key] == 1:
                values[key] = [value[0]] * n_rows
            else:
                values[key] = value

        parameters = []
        for row in range(n_rows):
            line = []
            for key, value in values.items():
                line.append(value[row])
            parameters.append(line)

        columns = ', '.join(kwargs.keys())
        places = ', '.join(['?'] * len(values.keys()))

        self.cursor.executemany(
            f'INSERT INTO {self.table} ({columns}) VALUES ({places})',
            parameters
        )

    def length_of_values(self, values: Any) -> int:
        """Return the length of the values argument.

        Parameters
        ----------
        values : Any
            The values, which might be a string, single value, list, tuple,
            etc.

        Returns
        -------
        length : int
            The length of the values. 0 indicates a scalar
        """
        if isinstance(values, str):
            return 0
        else:
            try:
                return len(values)
            except TypeError:
                return 0

    def to_dataframe(self):
        """Return the contents of the table as a Pandas Dataframe."""
        data = {}
        for line in self.cursor.execute(f'SELECT rowid, * FROM {self.table}'):
            data[line[0]] = line[1:]
        columns = [x[0] for x in self.cursor.description[1:]]

        df = pandas.DataFrame.from_dict(data, orient='index', columns=columns)

        return df

    def diff(self, other):
        """Difference between this table and another

        Parameters
        ----------
        other : _Table
            The other table to diff against

        Result
        ------
        result : Dict
            The differences, decribed in a dictionary
        """
        result = {}

        # Check the columns
        attributes = self.attributes
        columns = set(attributes)

        other_attributes = other.attributes
        other_columns = set(other_attributes)

        if columns == other_columns:
            column_def = 'rowid, *'
        else:
            added = columns - other_columns
            if len(added) > 0:
                result['columns added'] = list(added)
            removed = other_columns - columns
            if len(removed) > 0:
                result['columns removed'] = list(removed)

            in_common = other_columns & columns
            if len(in_common) > 0:
                column_def = 'rowid, ' + ', '.join(in_common)
            else:
                # No columns shared
                return result

        # Need to check the contents of the tables. See if they are in the same
        # database or if we need to attach the other database temporarily.
        filename = self.system.filename
        other_filename = other.system.filename
        if filename != other_filename:
            # Attach the previous database in order to do comparisons.
            self.cursor.execute(f"ATTACH DATABASE '{other_filename}' AS other")
            other_table = f'other.{other.table}'
        else:
            other_table = other.table
        table = self.table

        changed = {}
        last = None
        for row in self.db.execute(
            f"""
            SELECT {column_def} FROM
            (
            SELECT {column_def} FROM {other_table}
            EXCEPT
            SELECT {column_def} FROM {table}
            )
            UNION ALL
            SELECT {column_def} FROM
            (
            SELECT {column_def} FROM {table}
            EXCEPT
            SELECT {column_def} FROM {other_table}
            )
            ORDER BY rowid
            """
        ):
            if last is None:
                last = row
            elif row['rowid'] == last['rowid']:
                # changes = []
                changes = set()
                for k1, v1, v2 in zip(last.keys(), last, row):
                    if v1 != v2:
                        # changes.append((k1, v1, v2))
                        changes.add((k1, v1, v2))
                changed[row['rowid']] = changes
                last = None
            else:
                last = row
        if len(changed) > 0:
            result['changed'] = changed

        # See about the rows added
        added = {}
        for row in self.db.execute(
            f"""
            SELECT rowid, * FROM {table}
            WHERE rowid NOT IN (SELECT rowid FROM {other_table})
            """
        ):
            added[row['rowid']] = row[1:]

        if len(added) > 0:
            result['columns in added rows'] = row.keys()[1:]
            result['added'] = added

        # See about the rows removed
        removed = {}
        for row in self.db.execute(
            f"""
            SELECT rowid, * FROM {other_table}
            WHERE rowid NOT IN (SELECT rowid FROM {table})
            """
        ):
            removed[row['rowid']] = row[1:]

        if len(removed) > 0:
            result['columns in removed rows'] = row.keys()[1:]
            result['removed'] = removed

        # Detach the other database if needed
        if filename != other_filename:
            self.cursor.execute("DETACH DATABASE other")

        return result


if __name__ == '__main__':  # pragma: no cover
    import json
    import os.path
    import tempfile
    import timeit
    import time

    from molsystem import System
    import numpy

    x = [1.0, 2.0, 3.0]
    y = [4.0, 5.0, 6.0]
    z = [7.0, 8.0, 9.0]
    atno = [8, 1, 1]

    with tempfile.TemporaryDirectory() as tmpdirname:
        print(tmpdirname)
        filepath = os.path.join(tmpdirname, 'system1.db')
        system1 = System(filename=filepath)

        table1 = system1.create_table('table1')
        table1.add_attribute('atno', coltype='int')
        for column in ('x', 'y', 'z'):
            table1.add_attribute(column, coltype='float')
        with table1 as tmp:
            tmp.append(x=x, y=y, z=z, atno=atno)

        table2 = system1.create_table('table2')
        table2.add_attribute('atno', coltype='int')
        for column in ('x', 'y', 'z'):
            table2.add_attribute(column, coltype='float')
        x1 = x
        x1[0] = 0.0
        with table2 as tmp:
            tmp.append(x=x1, y=y, z=z, atno=[10, 1, 1])
            tmp.append(x=20.0, y=21.0, z=22.0, atno=12)

        print('table1 -> table2')
        diffs = table2.diff(table1)
        print(json.dumps(diffs, indent=4))

        print('table2 -> table1')
        diffs = table1.diff(table2)
        print(json.dumps(diffs, indent=4))

    exit()

    def run(nper=1000, nrepeat=100) -> None:
        with tempfile.TemporaryDirectory() as tmpdirname:
            filepath = os.path.join(tmpdirname, 'seamm.db')
            system = System(filename=filepath)
            table = system.create_table('table1')
            for column in ('a', 'b', 'c'):
                table.add_attribute(column, coltype='float')

            a = numpy.random.uniform(low=0, high=100, size=nper)
            b = numpy.random.uniform(low=0, high=100, size=nper)
            c = numpy.random.uniform(low=0, high=100, size=nper)

            for i in range(0, nrepeat):
                table.append(a=a.tolist(), b=b.tolist(), c=c.tolist())
                a += 10.0
                b += 5.0
                c -= 3.0

    nrepeat = 1000
    nper = 100
    t = timeit.timeit(
        "run(nper={}, nrepeat={})".format(nper, nrepeat),
        setup="from __main__ import run",
        timer=time.time,
        number=1
    )

    print("Creating {} rows took {:.3f} s".format(nper * nrepeat, t))
