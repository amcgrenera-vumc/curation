"""
AOU - De-identification Engine
Steve L. Nyemba <steve.l.nyemba@vanderbilt.edu>

This engine will run de-identificataion rules againsts a given table, certain
rules are applied to all tables (if possible).  We have divided rules and application
of the rules, in order to have granular visibility into what is happening.

DESIGN:

    - The solution:
    The Application of most of the rules are handled in the SQL projection, this
    allows for simpler jobs with no risk of limitations around joins imposed by big-query.
    By dissecting operations in this manner it is possible to log the nature of an
    operation on a given attribute as well as pull some sample data to illustrate it.

    We defined a vocabulary of rule specifications:
        -fields         Attributes upon which a given rule can be applied
        -values         conditional values that determine an out-come of a rule (can be followed by an operation like REGEXP)
                            If followed by "apply": "REGEXP" the the values are assumed to be applied using regular expressions
                            If NOT followed by anything the values are assumed to be integral values and and the IN operator in used instead
        -into           outcome related to a rule
        -key_field      attribute to be used as a filter that can be specified by value_field or values
        -value_field    value associated with a key_field
        -on             suggests a meta table and will have filter condition when generalization or a field name for row based suppression
        -copy_to        defined for generalizations.  uses the values associated with one field
                        to update another field.  to the user, it acts like a "copy new value to this field too" rule
        -dataset        defined for generalizations using aggregating rules.  helps develop the sub-query used
                        by the aggregate.  ':idataset' is default.  uses the dataset name
        -alias          defined for generalizations using aggregating rules.  allows a sub-query to
                        construct a temporary table named by this field
        -key_row        defined for generlizations using aggregating rules.  allows a sub-query to
                        define which field to use for joins


    Overall there are rules that suggest what needs to happen on values, and there
    is a fine specifying how to apply the rule on a given table.

    - The constraints:

        1. Bigquery is designed to be used as a warehouse not an RDBMS.That being said
            a. it lends itself to uncontrollable information redundancies and proliferation.
            b. There is not referential integrity support in bigquery.
            As a result thre is no mechanism that guarantees data-integrity.


        2. We have a method "simulate" that acts as a sampler to provide some visibility
        into what this engine has done given an attribute and the value space of the data.
        This potentially adds to data redundancies.  It must remain internal.

    LIMITATIONS:
        - The engine is not able to validate the rules without having to submit the job.
        - The engine can not simulate complex cases.  It's intent is to help in
        providing information about basic scenarios,
        - The engine does not resolve issues of consistency with data for instance:
        if a record has M, F on two fields for gender ... this issue is out of the scope of deid.
        Because it relates to data-integrity.

    USAGE:

        python aou.py --rules <path.json> --idataset <name> --private_key <file> --table <table.json> --action [submit, simulate|debug] [--cluster] [--log <path>]
        --rule  will point to the JSON file contianing rules
        --idataset  name of the input dataset (an output dataset with suffix _deid will be generated)
        --table     path of that specify how rules are to be applied on a table
        --private_key   service account file location
        --pipeline      specifies operations and the order in which operations are to be undertaken.
                        Operations should be comma separated
                        By default the pipeline is generalize, suppress, shift, compute
        --age-limit     This parameter is optional and sets the age limit by default it will apply 89 years
        --action        what to do:
                        simulate    will generate simulation without creating an output table
                        submit      will create an output table
                        debug       will just print output without simulation or submit (runs alone)
"""
from datetime import datetime
import json
import os
import time

from google.cloud import bigquery as bq
from google.oauth2 import service_account
import numpy as np
import pandas as pd

import bq_utils
import constants.bq_utils as bq_consts
from parser import parse_args
from press import Press


def milliseconds_since_epoch():
    """
    Helper method to get the number of milliseconds from the epoch to now

    :return:  an integer number of milliseconds
    """
    return int((datetime.utcnow() - datetime(1970, 1, 1)).total_seconds() * 1000)


class AOU(Press):
    def __init__(self, **args):
        args['store'] = 'bigquery'
        Press.__init__(self, **args)
        self.private_key = args.get('private_key', '')
        self.credentials = service_account.Credentials.from_service_account_file(self.private_key)
        self.odataset = self.idataset + '_deid'
        self.partition = args.get('cluster', False)
        self.priority = args.get('interactive', 'BATCH')

        if 'shift' in self.deid_rules:
            #
            # Minor updates that are the result of a limitation as to how rules are specified.
            # @TODO: Improve the rule specification language
            shift_days = ('SELECT shift from {idataset}.deid_map '
                          'WHERE deid_map.person_id = {tablename}.person_id'
                          .format(idataset=self.idataset, tablename=self.tablename))
            self.deid_rules['shift'] = json.loads(json.dumps(self.deid_rules['shift']).replace(":SHIFT", shift_days))

    def initialize(self, **args):
        Press.initialize(self, **args)
        age_limit = args['age_limit']
        max_day_shift = args['max_day_shift']
        million = 1000000
        map_tablename = self.idataset + ".deid_map"
        sql = ("SELECT DISTINCT person_id, EXTRACT(YEAR FROM CURRENT_DATE()) - year_of_birth as age "
               "FROM {idataset}.person ORDER BY 2"
               .format(idataset=self.idataset))
        person_table = self.get(sql=sql)
        self.log(module='initialize',
                 subject=self.get_tablename(),
                 action='patient-count',
                 value=person_table.shape[0])
        map_table = pd.DataFrame()

        # only need to create this mapping table if an observation table exists
        if self.get_tablename().lower() == 'observation':
            self.map_questionnaire_response_ids(million)

        if person_table.shape[0] > 0:
            person_table = person_table[person_table.age < age_limit]
            map_table = self.get(sql="SELECT * FROM " + map_tablename)
            if map_table.shape[0] > 0 and map_table.shape[0] == person_table.shape[0]:
                #
                # There is nothing to be done here
                # @TODO: This weak post-condition is not enough nor consistent
                #   - consider using a set operation
                #
                pass
            else:
                # create the deid_map table.  set upper and lower bounds of the research_id array
                records = person_table.shape[0]
                lower_bound = million
                upper_bound = lower_bound + (10 * records)
                map_table = pd.DataFrame({"person_id": person_table['person_id'].tolist()})

                # generate random research_ids
                research_id_array = np.random.choice(np.arange(lower_bound, upper_bound), records, replace=False)

                # throw in some extra, non-deterministic shuffling
                for _ in range(milliseconds_since_epoch() % 5):
                    np.random.shuffle(research_id_array)
                map_table['research_id'] = research_id_array

                # generate date shift values
                shift_array = np.random.choice(np.arange(1, max_day_shift), records)

                # throw in some extra, non-deterministic shuffling
                for _ in range(milliseconds_since_epoch() % 5):
                    np.random.shuffle(shift_array)
                map_table['shift'] = shift_array

                # write this to bigquery.
                map_table.to_gbq(map_tablename, credentials=self.credentials, if_exists='fail')
        else:
            print ("Unable to initialize Deid.  "
                   "Check configuration files, parameters, and credentials.")

        #
        # @TODO: Make sure that what happened here is logged and made available
        #   - how many people are mapped
        #   - how many people are not mapped
        #   - was the table created or not
        #
        self.log(module='initialize',
                 subject=self.get_tablename(),
                 action='mapped-patients',
                 value=map_table.shape[0])
        return person_table.shape[0] > 0 or map_table.shape[0] > 0


    def map_questionnaire_response_ids(self, lower_bound):
        map_tablename = self.idataset + ".deid_questionnaire_response_map"
        sql = ("SELECT DISTINCT o.questionnaire_response_id "
               "FROM {idataset}.observation as o "
               "WHERE o.questionnaire_response_id IS NOT NULL "
               "ORDER BY 1"
               .format(idataset=self.idataset))
        observation_table = self.get(sql=sql)
        self.log(module='initialize',
                 subject=self.get_tablename(),
                 action='questionnaire-response-id-count',
                 value=observation_table.shape[0])
        map_table = pd.DataFrame()

        if observation_table.shape[0] > 0:
            map_table = self.get(sql="SELECT * FROM " + map_tablename)
            if map_table.shape[0] > 0 and map_table.shape[0] == observation_table.shape[0]:
                #
                # There is nothing to be done here
                # @TODO: This weak post-condition is not enough nor consistent
                #   - consider using a set operation
                #
                pass
            else:
                # create the deid_map table.  set upper and lower bounds of the research_id array
                records = observation_table.shape[0]
                upper_bound = lower_bound + (10 * records)
                qr_id_list = observation_table['questionnaire_response_id'].tolist()
                map_table = pd.DataFrame({"questionnaire_response_id": qr_id_list})

                # generate random research_questionnaire_response_ids
                research_id_array = np.random.choice(np.arange(lower_bound, upper_bound), records, replace=False)

                # throw in some extra, non-deterministic shuffling
                for _ in range(milliseconds_since_epoch() % 5):
                    np.random.shuffle(research_id_array)
                map_table['research_response_id'] = research_id_array

                # write this to bigquery.
                map_table.to_gbq(map_tablename, credentials=self.credentials, if_exists='fail')
        else:
            self.log(module="initialize",
                     subject=self.get_tablename(),
                     action="questionnaire_response_id_count",
                     value="No questionnaire_response_ids found.")


    def update_rules(self):
        """
        This will add rules that are to be applied by default to the current table
        @TODO: Make sure there's a way to specify these in the configuration
        """
        df = self.get(limit=1)
        columns = df.columns.tolist()
        if 'suppress' not in self.info:
            self.info['suppress'] = []
        #
        # Relational attributes that require suppression are inventoried this allows automatic suppression
        # It's done to make the engine more usable (by minimizing the needs for configuration)
        #
        rem_cols = True
        for rule in self.info['suppress']:
            if 'rules' in rule and rule['rules'].endswith('DEMOGRAPHICS-COLUMNS'):
                rem_cols = False
                rule['table'] = self.get_tablename()
                rule['fields'] = columns

        if rem_cols:
            self.info['suppress'] += [{"rules": "@suppress.DEMOGRAPHICS-COLUMNS",
                                       "table": self.get_tablename(),
                                       "fields": df.columns.tolist()}]

        date_columns = [name for name in columns if set(['date', 'time', 'datetime']) & set(name.split('_'))]
        #
        # shifting date attributes can be automatically done for relational fields
        # by looking at the field name of the data-type
        # @TODO: consider looking at the field name, changes might happen all of a sudden
        #
        if date_columns:
            date = {}
            datetime_field = {}

            for name in date_columns:
                if 'datetime' in name or 'time' in name:
                    if 'fields' not in datetime_field:
                        datetime_field['fields'] = []
                    datetime_field['fields'].append(name)
                    datetime_field['rules'] = '@shift.datetime'
                elif 'date' in name:
                    if 'fields' not in date:
                        date['fields'] = []

                    date['fields'].append(name)
                    date['rules'] = '@shift.date'
            _toshift = []

            if date and datetime_field:
                _toshift = [date, datetime_field]
            elif date or datetime_field:
                _toshift = [date] if date else [datetime_field]

            if 'shift' not in self.info:
                self.info['shift'] = []

            if _toshift:
                self.log(module='update-rules', subject=self.get_tablename(), object='shift', fields=_toshift)
                self.info['shift'] += _toshift

        #
        # let's check for the person_id
        has_compute_id = False
        if 'compute' not in self.info:
            self.info['compute'] = []
        else:
            for rule_dict in self.info.get('compute', {}):
                if '@compute.id' in rule_dict.get('rules'):
                    has_compute_id = True

        if not has_compute_id and 'person_id' in columns:
            self.info['compute'] += [
                {
                    "rules": "@compute.id",
                    "fields": ["person_id"],
                    "table": ":idataset.deid_map as map_user",
                    "key_field": "map_user.person_id",
                    "value_field": self.tablename + ".person_id"
                }
            ]

    def get(self, **args):
        """
        This function will execute a query to a data-frame (for easy handling)
        """
        if 'sql' in args:
            sql = args['sql']
        else:
            sql = ("SELECT * FROM {idataset}.{tablename}"
                   .format(idataset=self.idataset, tablename=self.tablename))

        if 'limit' in args:
            sql = sql + " LIMIT " + str(args['limit'])

        try:
            df = pd.read_gbq(sql, credentials=self.credentials, dialect='standard')
            return df
        except Exception as error:
            self.log(module='get', action='error', value=error.message)
            print error.message

        return pd.DataFrame()

    def submit(self, sql, create):
        table_name = self.get_tablename()
        client = bq.Client.from_service_account_json(self.private_key)
        #
        # Let's make sure the out dataset exists
        datasets = list(client.list_datasets())
        found = np.sum([1  for dataset in datasets if dataset.dataset_id == self.odataset])
        if not found:
            dataset = bq.Dataset(client.dataset(self.odataset))
            client.create_dataset(dataset)

        # create the output table
        if create:
            self.log(module='submit', subject=self.get_tablename(),
                     action='creating new table')
            bq_utils.create_standard_table(
                self.tablename, self.tablename, drop_existing=True, dataset_id=self.odataset
            )
            write_disposition = bq_consts.WRITE_EMPTY
        else:
            write_disposition = bq_consts.WRITE_APPEND
            self.log(module='submit', subject=self.get_tablename(),
                     action='not creating new table')

        job = bq.QueryJobConfig()
        job.destination = client.dataset(self.odataset).table(self.tablename)
        job.use_query_cache = True
        job.allow_large_results = True
        job.write_disposition = write_disposition
        if self.partition:
            job._properties['timePartitioning'] = {'type': 'DAY'}
            job._properties['clustering'] = {'field': 'person_id'}

        job.priority = self.priority
        job.dry_run = True

        self.log(module='submit-job',
                 subject=self.get_tablename(),
                 action='dry-run',
                 value={'priority': self.priority, 'parition': self.partition})

        logpath = os.path.join(self.logpath, self.idataset)
        try:
            os.makedirs(logpath)
        except OSError:
            # log path already exists and we don't care
            pass

        r = client.query(sql, location='US', job_config=job)
        if r.errors is None and r.state == 'DONE':
            job.dry_run = False

            r = client.query(sql, location='US', job_config=job)
            self.log(module='submit',
                     subject=self.get_tablename(),
                     action='submit-job',
                     table=table_name,
                     status='pending',
                     value=r.job_id,
                     object='bigquery')
            self.wait(client, r.job_id)
        else:
            self.log(module='submit',
                     subject=self.get_tablename(),
                     action='submit-job',
                     table=table_name,
                     status='error',
                     value=r.errors)
            print (r.errors)

    def wait(self, client, job_id):
        self.log(module='wait', subject=self.get_tablename(), action="sleep", value=job_id)
        status = 'NONE'

        while True:
            status = client.get_job(job_id).state

            if status == 'DONE':
                break
            else:
                time.sleep(5)

        self.log(module='wait', action='awake', status=status)


def main(raw_args=None):
    sys_args = parse_args(raw_args)

    handle = AOU(**sys_args)

    if handle.initialize(age_limit=sys_args.get('age-limit'), max_day_shift=365):
        handle.do()
    else:
        print ("Unable to initialize process ")
        print ("\tEnsure that the parameters are correct")


if __name__ == '__main__':
    main()
