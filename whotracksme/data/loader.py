
from datetime import datetime
from urllib.parse import quote_plus
import io
import pkg_resources
import pandas as pd
import sqlite3
from collections import namedtuple
from operator import itemgetter
from whotracksme.data.db import load_tracker_db, create_tracker_map, WhoTracksMeDB, DATA_COLUMNS


def asset_exists(name):
    return pkg_resources.resource_exists('whotracksme.data', f'assets/{name}')


def asset_stream(name):
    stream = pkg_resources.resource_stream(
        'whotracksme.data',
        f'assets/{name}',
    )
    in_memory_stream = io.BytesIO(stream.read())
    stream.close()
    return in_memory_stream


def list_available_months(region="global"):
    months = []
    for asset in pkg_resources.resource_listdir('whotracksme.data', 'assets'):
        try:
            month = datetime.strptime(asset, '%Y-%m')
        except ValueError:
            pass
        else:
            # Making sure the region is availabe in a given month
            if pkg_resources.resource_isdir('whotracksme.data', f'assets/{asset}/{region}'):
                months.append(asset)
    return months


class DataSource:
    def __init__(self, region="global", populate=True):
        self.data_months = sorted(list_available_months(region=region))
        if populate:
            print('data available for months:\n├──', "\n├── ".join(self.data_months))

        # Add demographics info to trackers and companies
        self.db = WhoTracksMeDB()

        # self.sites_trackers = SitesTrackers(
        #     data_months=[max(self.data_months)],
        #     tracker_info=self.app_info,
        #     region=region,
        #     db=self.db
        # )
        self.trackers = Trackers(
            data_months=self.data_months,
            region=region,
            db=self.db,
            populate=populate,
        )
        # self.companies = Companies(
        #     data_months=self.data_months,
        #     region=region,
        #     db=self.db
        # )
        # self.sites = Sites(
        #     data_months=self.data_months,
        #     trackers=self.sites_trackers,
        #     region=region,
        #     db=self.db
        # )

    @staticmethod
    def normalize_url(url_substring):
        return quote_plus(url_substring.replace('/', ' ')).lower()

    def url_for(self, entity, id, path_to_root='.'):
        if entity == 'tracker':
            return f'{path_to_root}/trackers/{self.normalize_url(id)}.html'
        elif entity == 'company':
            return f'{path_to_root}/companies/{self.normalize_url(id)}.html'
        elif entity == 'site':
            return f'{path_to_root}/websites/{self.normalize_url(id)}.html'.lower()
        elif entity == 'blog':
            return f'{path_to_root}/blog/{self.normalize_url(id)}.html'

    def get_company_name(self, id):
        result = self.db.connection.execute('SELECT name FROM companies WHERE id = ?', (id,)).fetchone()
        if result is not None:
            return result[0]
        return id

def parse_date(date_string):
    return datetime.strptime(date_string, '%Y-%m')


DataRowTypes = {
    "trackers": namedtuple('TrackerDataPoint', 'id, month, country, tracker, company_id, category_id, category,' + ','.join(DATA_COLUMNS['trackers'])),
}

class SQLDataLoader:

    def __init__(self, data_months, name, db, region='global', id_column=None, extra_columns=[]):
        self.name = name
        self.last_month = max(data_months)
        self.db = db
        self.id_column = id_column
        self.region = region
        # infer table structure
        self.columns = [row[1] for row in self.db.connection.execute(f'PRAGMA table_info("{self.name}_data")')]
        self.rowType = namedtuple(f'{self.name}DataPoint', ','.join(['id'] + self.columns + extra_columns))

    def get_data_query(self):
        return f'''
            SELECT
                {self.id_column} AS id,
                {','.join(self.columns)}
                {',' if len(self.extra_column_select) > 0 else ''}
                {','.join(self.extra_column_select)}
            FROM {self.name}_data
            {self.extra_joins}
        '''

    def sort_by(self, metric="reach", descending=True):
        cursor = self.db.connection.cursor()
        columns = ','.join(DATA_COLUMNS['trackers'])
        cursor.execute(f'''
            {self.get_data_query()}
            WHERE country=? AND month = ?
            ORDER BY "{metric}" {'DESC' if descending else 'ASC'}
        ''', (self.region, self.last_month))
        return list(map(self.rowType._make, cursor.fetchall()))

    def get_snapshot(self, month=None):
        cursor = self.db.connection.cursor()
        cursor.execute(f'''
            {self.get_data_query()}
            WHERE country = ? AND month = ?
        ''', (self.region, month or self.last_month))
        return list(map(self.rowType._make, cursor.fetchall()))

    def iter(self):
        for row in self.get_snapshot():
            yield row.id, row

    def get_datapoint(self, id, month=None):
        result = self.db.connection.execute(f'''
            {self.get_data_query()}
            WHERE country= ? AND month = ? AND tracker = ?
        ''', (self.region, month or self.last_month, id)).fetchone()
        if result is not None:
            return self.rowType._make(result)
        return None

class Trackers(SQLDataLoader):

    def __init__(self, data_months, db, region='global', populate=True):
        super().__init__(data_months, name='trackers', db=db, region=region, id_column='tracker',
                         extra_columns=['company_id', 'category_id', 'category'])
        self.extra_column_select = [
            'trackers.company_id',
            'trackers.category_id',
            'categories.name AS category',
        ]
        self.extra_joins = '''
            LEFT JOIN trackers ON trackers.id = trackers_data.tracker
            LEFT JOIN categories ON trackers.category_id = categories.id
        '''
        self.info = {}

        if populate:
            for month in data_months:
                self.db.load_data('trackers', self.region, month)
            self.db.load_data('sites_trackers', self.region, self.last_month)
            self.db.load_data('sites', self.region, self.last_month)

    # Summary methods across all trackers
    # -----------------------------------

    def summary_stats(self):
        """
        Returns: Summary stats across all trackers.

        """
        # snapshot of last month in the data
        stats = self.get_snapshot()
        cursor = self.db.connection.cursor()
        cursor.execute('''
            SELECT
            COUNT(tracker) as count,
            COUNT(CASE WHEN reach > 0.001 THEN 1 ELSE NULL END) as gt01,
            COUNT(CASE WHEN cookies > 0.2 THEN 1 ELSE NULL END) as by_cookies,
            COUNT(CASE WHEN bad_qs > 0.1 THEN 1 ELSE NULL END) as by_fingerprinting,
            AVG(content_length) as data
            FROM trackers_data
            WHERE country=? AND month = ?
        ''', (self.region, self.last_month))
        result = cursor.fetchone()
        return {
            'count': result[0],
            'gt01': result[1],
            'by_cookies': float(result[2]) / result[0],
            'by_fingerprinting': float(result[3]) / result[0],
            'data': result[4]
        }

    # Methods for a specific Tracker
    # ------------------------------
    def get_tracker(self, id):
        if id in self.info:
            return self.info[id]

        cursor = self.db.connection.cursor()
        cursor.execute('''
            SELECT
                t.id,
                t.name,
                c.name AS category,
                t.website_url,
                t.ghostery_id,
                t.company_id,
                iab.id AS iab_vendor,
                truste.type AS truste_type,
                truste.description,
                truste.privacy_url,
                com.privacy_url
            FROM trackers AS t
            JOIN categories AS c ON c.id = t.category_id
            LEFT JOIN companies as com ON com.id = t.company_id
            LEFT JOIN iab_vendors as iab ON iab.tracker = t.id
            LEFT JOIN truste_companies as truste ON truste.tracker = t.id
            WHERE t.id = ?
            ''', (id,))
        cols = ['id', 'name', 'category', 'website_url', 'ghostery_id', 'company_id', 'iab_vendor',
            'truste_type', 'description', 'truste_privacy_url',
            'privacy_url']
        row = cursor.fetchone()
        tracker_info = {c: row[i] for i, c in enumerate(cols)}

        cursor.execute('''
            SELECT tracker, MIN(month), MAX(month)
            FROM trackers_data
            WHERE country = ? AND tracker = ?
            GROUP BY tracker
        ''', (self.region, id))
        date_range = cursor.fetchone()
        if date_range is not None:
            tracker_info['date_range'] = [parse_date(date_range[1]), parse_date(date_range[2])]

        overview = self.get_datapoint(id)
        if overview is not None:
            tracker_info['overview'] = overview._asdict()

        self.info[id] = tracker_info
        return tracker_info

    def get_name(self, id):
        return self.get_tracker(id).get('name')

    def get_rank(self, id):
        return self.get_tracker(id).get('overview', {}).get('reach_rank')

    def get_rank_label(self, id):
        """
        Args:
            id: id of tracker

        Returns: Label based on rank

        """
        r = self.get_rank(id)
        if r < 3:
            return 'Dangerously prevalent'
        if 3 <= r < 11:
            return 'Extremely prevalent'
        if 11 <= r < 50:
            return 'Very prevalent'
        if 51 <= r <= 100:
            return 'Commonly prevalent'
        if 101 <= r:
            return 'Relatively prevalent'

    def get_tracking_methods(self, id):
        """
        Args:
            id: id of tracker to access

        Returns: {'cookies:: bool, 'fingerprinting':: bool}
                based on chosen threshold by privacy team.

        """
        methods = {
            'cookies': False,
            'fingerprinting': False
        }
        if self.get_tracker(id).get('overview', {}).get('cookies') > 0.2:
            methods['cookies'] = True
        if self.get_tracker(id).get('overview', {}).get('bad_qs') > 0.1:
            methods['fingerprinting'] = True
        return methods

    def get_reach(self, id):
        cursor = self.db.connection.cursor()
        cursor.execute('''
            SELECT month, reach, site_reach_top10k FROM trackers_data
            WHERE country = ? AND tracker = ?
            ORDER BY month ASC
        ''', (self.region, id))
        reach = []
        months = []
        site_reach = []
        for row in cursor.fetchall():
            m, r, s = row
            reach.append(r)
            months.append(parse_date(m))
            if s is not None:
                site_reach.append(s)
        return {
            'page': reach,
            'ts': months,
            'site': site_reach,
        }

    def get_presence_by_site_category(self, id):
        cursor = self.db.connection.cursor()
        cursor.execute('''
            SELECT category, COUNT(*) AS count
            FROM sites_trackers_data AS st
            JOIN sites_data ON
                st.month = sites_data.month
                AND st.country = sites_data.country
                AND st.site = sites_data.site
            WHERE st.month = ?
                AND st.country = ?
                AND tracker = ?
            GROUP BY sites_data.category
            ORDER BY count DESC
        ''', (self.last_month, self.region, id))
        results = cursor.fetchall()
        site_count = sum(map(itemgetter(1), results))
        return [(category, 100 * float(count) / site_count) for category, count in results]

    def similar_trackers(self, id, n=4):
        """
        Args:
            id: id of tracker for which similar trackers will be found
            n: number of similar trackers to find

        Returns:
            top_n: list of similar trackers, each having an id
                   and the company_id
        """
        tracker = self.get_tracker(id)

        cursor = self.db.connection.cursor()
        cursor.execute(f'''
            SELECT tracker, company_id
            FROM trackers_data
            JOIN trackers ON trackers.id = trackers_data.tracker
            WHERE month = ? AND country = ?
                AND category_id = ?
                AND tracker != ?
            ORDER BY reach DESC
            LIMIT {n}
        ''', (self.last_month, self.region, tracker['overview']['category_id'], id))
        return [{
            'id': t,
            'company_id': cid
        } for t, cid in cursor.fetchall()]

    def get_domains(self, id):
        try:
            if self.get_tracker(id) is not None:
                return self.get_tracker(id).get('domains', [])
            else:
                return []
        except Exception as e:
            print(e)

    def iter_sites(self, id):
        for site in self.sites.get_tracker(id).itertuples():
            yield site


class Sites(SQLDataLoader):
    def __init__(self, data_months, trackers, region='global', id_column='site'):
        super().__init__(data_months, name='sites', region=region)
        self.trackers = trackers
        self.df['id'] = self.df['site']
        # site -> category mapping
        self.site_category = {
            row.id: row.category for row in self.get_snapshot().itertuples()
        }

    # Summary methods across all sites
    # --------------------------------

    def summary_stats(self):
        """
        Returns: aggregate tracker statistics across all sites in database

        """
        stats = self.get_snapshot()
        return {
            'count': len(stats),
            "have_trackers": stats.tracked.mean(),
            'gt10': len(stats[stats.trackers >= 10]),
            "average_nr_trackers": stats.trackers.mean(),
            "tracker_requests": int(stats.requests_tracking.mean()),
            'data': stats.content_length.mean()
        }

    # Methods for one specific site
    # -----------------------------
    def get_site(self, id):
        return self.df[self.df.site == id]

    def get_name(self, id):
        # NOTE: This is weird
        return id if len(self.get_site(id)) > 0 else None

    def trackers_on_site(self, id, trackers, companies):
        """
        Args:
            id: a site dict from self._sites
            trackers: DataSource.trackers
            companies: DataSource.companies

        Returns:
            tracker :: dict,
            category :: string,
            company_name :: string
        """

        for t in self.trackers.get_site(id).sort_values('site_proportion', ascending=False).itertuples():
            tracker_id = t.tracker
            try:
                tracker = trackers.get_tracker(tracker_id)
                tracker['frequency'] = t.site_proportion
            except TypeError:
                continue
            category = tracker.get('category', 'unknown')
            if category == 'extensions' or category is None:
                continue

            cid = tracker.get('company_id')
            company_name = companies.get(cid, {}).get('name') or tracker['name']

            yield (tracker, category, company_name)

    def mean_trackers_timeseries(self, id):
        """
        Args:
            id: id, e.g.: ebay.de

        Returns: [(ts0, mean_trackers0, ... ]
        """
        return [(s.get('ts'), s.get('mean_trackers'))
                for s in self.get_site(id).get('history')]


class SitesTrackers(SQLDataLoader):

    def __init__(self, data_months, tracker_info, region='global'):
        super().__init__(data_months, name='sites_trackers', region=region)

        self.df['company_id'] = pd.Series(
            [tracker_info.get(tracker, {}).get('company_id', tracker)
            for tracker in self.df.tracker], index=self.df.index)

    def get_tracker(self, tracker):
        return self.df[self.df.tracker == tracker]

    def get_site(self, site):
        return self.df[self.df.site == site]


class Companies(SQLDataLoader):

    def __init__(self, data_months, company_info, tracker_info, region='global'):
        super().__init__(data_months, name='companies', region=region, id_column='company')
        self.df['id'] = self.df['company']
        self.df['name'] = pd.Series([
            company_info.get(row.company, tracker_info.get(row.company, {})).get('name', row.company)
            for row in self.df.itertuples()],
            index=self.df.index)
