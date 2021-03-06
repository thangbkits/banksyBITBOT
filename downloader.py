import argparse
import asyncio
import datetime
import gc
import os
import sys
import gzip
from io import BytesIO
from time import sleep
from time import time
from urllib.request import urlopen
from zipfile import ZipFile

import numpy as np
import pandas as pd
from dateutil import parser

from procedures import prep_config, make_get_filepath, create_binance_bot, create_bybit_bot, print_, add_argparse_args
from pure_funcs import ts_to_date, get_dummy_settings


class Downloader:
    """
    Downloader class for tick data. Fetches data from specified time until now or specified time.
    """

    def __init__(self, config: dict):
        self.fetch_delay_seconds = 0.75
        self.config = config
        self.price_filepath = os.path.join(config["caches_dirpath"], f"{config['session_name']}_price_cache.npy")
        self.buyer_maker_filepath = os.path.join(config["caches_dirpath"],
                                                 f"{config['session_name']}_buyer_maker_cache.npy")
        self.time_filepath = os.path.join(config["caches_dirpath"], f"{config['session_name']}_time_cache.npy")
        # self.qty_filepath = os.path.join(config["caches_dirpath"], f"{config['session_name']}_qty_cache.npy")
        self.tick_filepath = os.path.join(config["caches_dirpath"], f"{config['session_name']}_ticks_cache.npy")
        try:
            self.start_time = int(parser.parse(self.config["start_date"]).replace(
                tzinfo=datetime.timezone.utc).timestamp() * 1000)
        except Exception:
            raise Exception(f"Unrecognized date format for start time {config['start_date']}")
        self.end_time = self.config["end_date"]
        if self.end_time != -1:
            try:
                self.end_time = int(
                    parser.parse(self.end_time).replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
            except Exception:
                raise Exception(f"Unrecognized date format for end time {config['end_date']}")
        if self.config['exchange'] == 'binance':
            if 'spot' in self.config and self.config['spot']:
                self.daily_base_url = "https://data.binance.vision/data/daily/aggTrades/"
                self.monthly_base_url = "https://data.binance.vision/data/monthly/aggTrades/"
            else:
                market_type = 'cm' if config['inverse'] else 'um'
                self.daily_base_url = f"https://data.binance.vision/data/futures/{market_type}/daily/aggTrades/"
                self.monthly_base_url = f"https://data.binance.vision/data/futures/{market_type}/monthly/aggTrades/"
        elif self.config['exchange'] == 'bybit':
            self.daily_base_url = 'https://public.bybit.com/trading/'
        else:
            raise Exception(f"unknown exchange {config['exchange']}")

    def validate_dataframe(self, df: pd.DataFrame) -> tuple:
        """
        Validates a dataframe and detects gaps in it. Also detects missing trades in the beginning and end.
        @param df: Dataframe to check for gaps.
        @return: A tuple with following result: if missing values present, the cleaned dataframe, a dataframe with start and end of gaps.
        """
        df.sort_values("trade_id", inplace=True)
        df.drop_duplicates("trade_id", inplace=True)
        df.reset_index(drop=True, inplace=True)
        missing_end_frame = df["trade_id"][df["trade_id"].diff() != 1]
        gaps = pd.DataFrame()
        gaps["start"] = df.iloc[missing_end_frame[1:].index - 1]["trade_id"].tolist()
        gaps["end"] = missing_end_frame[1:].tolist()
        if missing_ids := df["trade_id"].iloc[0] % 100000 != 0:
            gaps.append({"start": df["trade_id"].iloc[0] - missing_ids, "end": df["trade_id"].iloc[0] - 1},
                        ignore_index=True)
        if missing_ids := df["trade_id"].iloc[-1] % 100000 != 99999:
            gaps.append({"start": df["trade_id"].iloc[-1], "end": df["trade_id"].iloc[-1] + (100000 - missing_ids - 1)},
                        ignore_index=True)
        missing_ids = df["trade_id"].iloc[0] % 100000
        if missing_ids != 0:
            gaps = gaps.append({"start": df["trade_id"].iloc[0] - missing_ids, "end": df["trade_id"].iloc[0] - 1},
                               ignore_index=True)
        missing_ids = df["trade_id"].iloc[-1] % 100000
        if missing_ids != 99999:
            gaps = gaps.append(
                {"start": df["trade_id"].iloc[-1], "end": df["trade_id"].iloc[-1] + (100000 - missing_ids - 1)},
                ignore_index=True)
        if gaps.empty:
            return False, df, gaps
        else:
            gaps["start"] = gaps["start"].astype(np.int64)
            gaps["end"] = gaps["end"].astype(np.int64)
            gaps.sort_values("start", inplace=True)
            gaps.reset_index(drop=True, inplace=True)
            gaps["start"] = gaps["start"].replace(0, 1)
            return True, df, gaps

    def read_dataframe(self, path) -> pd.DataFrame:
        """
        Reads a dataframe with correct data types.
        @param path: The path to the dataframe.
        @return: The read dataframe.
        """
        try:
            df = pd.read_csv(path,
                             dtype={"trade_id": np.int64, "price": np.float64, "qty": np.float64, "timestamp": np.int64,
                                    "is_buyer_maker": np.int8})
        except ValueError as e:
            df = pd.read_csv(path)
            df = df.drop("side", axis=1).join(pd.Series(df.side == "Sell", name="is_buyer_maker", index=df.index))
            df = df.astype({"trade_id": np.int64, "price": np.float64, "qty": np.float64, "timestamp": np.int64,
                            "is_buyer_maker": np.int8})
        return df

    def save_dataframe(self, df, filename, missing):
        """
        Saves a processed dataframe. Creates the name based on first and last trade id and first and last timestamp.
        Deletes dataframes that are obsolete. For example, when gaps were filled.
        @param df: The dataframe to save.
        @param filename: The current name of the dataframe.
        @param missing: If the dataframe had gaps.
        @return:
        """
        new_name = f'{df["trade_id"].iloc[0]}_{df["trade_id"].iloc[-1]}_{df["timestamp"].iloc[0]}_{df["timestamp"].iloc[-1]}.csv'
        if new_name != filename:
            print_(['Saving file', new_name])
            df.to_csv(os.path.join(self.filepath, new_name), index=False)
            new_name = ""
            try:
                os.remove(os.path.join(self.filepath, filename))
                print_(['Removed file', filename])
            except:
                pass
        elif missing:
            print_(['Replacing file', filename])
            df.to_csv(os.path.join(self.filepath, filename), index=False)
        else:
            new_name = ""
        return new_name

    def transform_ticks(self, ticks: list) -> pd.DataFrame:
        """
        Transforms tick data into a cleaned dataframe with correct data types.
        @param ticks: List of tick dictionaries.
        @return: Clean dataframe with correct data types.
        """
        df = pd.DataFrame(ticks)
        if not df.empty:
            df["trade_id"] = df["trade_id"].astype(np.int64)
            df["price"] = df["price"].astype(np.float64)
            df["qty"] = df["qty"].astype(np.float64)
            df["timestamp"] = df["timestamp"].astype(np.int64)
            df["is_buyer_maker"] = df["is_buyer_maker"].astype(np.int8)
            df.sort_values("trade_id", inplace=True)
            df.drop_duplicates("trade_id", inplace=True)
            df.reset_index(drop=True, inplace=True)
        return df

    def get_filenames(self) -> list:
        """
        Returns a sorted list of all file names in the directory.
        @return: Sorted list of file names.
        """
        return sorted([f for f in os.listdir(self.filepath) if f.endswith(".csv")],
                      key=lambda x: int(eval(x[:x.find("_")].replace(".cs", "").replace("v", ""))))

    def new_id(self, first_timestamp, last_timestamp, first_trade_id, length, start_time, prev_div):
        """
        Calculates a new id based on several parameters. Uses a weighted approach for more stability.
        @param first_timestamp: First timestamp in current result.
        @param last_timestamp: Last timestamp in current result.
        @param first_trade_id: First trade id in current result.
        @param length: The amount of trades in the current result.
        @param start_time: The time to look for.
        @param prev_div: Previous results of this function.
        @return: Estimated trade id.
        """
        div = int((last_timestamp - first_timestamp) / length)
        prev_div.append(div)
        forward = int((first_timestamp - start_time) / np.mean(prev_div))
        return max(1, int(first_trade_id - forward)), prev_div, forward

    async def find_time(self, start_time) -> pd.DataFrame:
        """
        Finds the trades according to the time.
        Uses different approaches for exchanges depending if time based fetching is supported.
        If time based searching is supported, directly fetch the data.
        If time based searching is not supported, start with current trades and move closer to start time based on estimation.
        @param start_time: Time to look for.
        @return: Dataframe with first trade later or equal to start time.
        """
        try:
            ticks = await self.bot.fetch_ticks_time(start_time)
            return self.transform_ticks(ticks)
        except:
            print_(['Finding id for start time...'])
            ticks = await self.bot.fetch_ticks()
            df = self.transform_ticks(ticks)
            highest_id = df["trade_id"].iloc[-1]
            prev_div = []
            first_ts = df["timestamp"].iloc[0]
            last_ts = df["timestamp"].iloc[-1]
            first_id = df["trade_id"].iloc[0]
            length = len(df)
            while not start_time >= first_ts or not start_time <= last_ts:
                loop_start = time()
                nw_id, prev_div, forward = self.new_id(first_ts, last_ts, first_id, length, start_time, prev_div)
                print_(['Current time span from', df["timestamp"].iloc[0], 'to', df["timestamp"].iloc[-1],
                        'with earliest trade id', df["trade_id"].iloc[0], 'estimating distance of', forward, 'trades'])
                if nw_id > highest_id:
                    nw_id = highest_id
                try:
                    ticks = await self.bot.fetch_ticks(from_id=int(nw_id), do_print=False)
                    df = self.transform_ticks(ticks)
                    if not df.empty:
                        first_ts = df["timestamp"].iloc[0]
                        last_ts = df["timestamp"].iloc[-1]
                        first_id = df["trade_id"].iloc[0]
                        length = len(df)
                        if nw_id == 1 and first_ts >= start_time:
                            break
                except Exception:
                    print("Failed to fetch or transform...")
                await asyncio.sleep(max(0.0, self.fetch_delay_seconds - time() + loop_start))
            print_(['Found id for start time!'])
            return df[df["timestamp"] >= start_time]

    def get_zip(self, base_url, symbol, date):
        """
        Fetches a full day of trades from the Binance repository.
        @param symbol: Symbol to fetch.
        @param date: Day to download.
        @return: Dataframe with full day.
        """
        print_(['Fetching', symbol, date])
        url = "{}{}/{}-aggTrades-{}.zip".format(base_url, symbol.upper(), symbol.upper(), date)
        df = pd.DataFrame(columns=['trade_id', 'price', 'qty', 'timestamp', 'is_buyer_maker'])
        try:
            resp = urlopen(url)
            with ZipFile(BytesIO(resp.read())) as my_zip_file:
                for contained_file in my_zip_file.namelist():
                    tf = pd.read_csv(my_zip_file.open(contained_file),
                                     names=['trade_id', 'price', 'qty', 'first', 'last', 'timestamp', 'is_buyer_maker'])
                    tf.drop(errors='ignore', columns=['first', 'last'], inplace=True)
                    tf["trade_id"] = tf["trade_id"].astype(np.int64)
                    tf["price"] = tf["price"].astype(np.float64)
                    tf["qty"] = tf["qty"].astype(np.float64)
                    tf["timestamp"] = tf["timestamp"].astype(np.int64)
                    tf["is_buyer_maker"] = tf["is_buyer_maker"].astype(np.int8)
                    tf.sort_values("trade_id", inplace=True)
                    tf.drop_duplicates("trade_id", inplace=True)
                    tf.reset_index(drop=True, inplace=True)
                    if df.empty:
                        df = tf
                    else:
                        df = pd.concat([df, tf])
        except Exception as e:
            print('Failed to fetch', date, e)
        return df

    async def find_df_enclosing_timestamp(self, timestamp, guessed_chunk=None):
        if guessed_chunk is not None:
            if guessed_chunk[0]['timestamp'] < timestamp < guessed_chunk[-1]['timestamp']:
                print_(['found id'])
                return self.transform_ticks(guessed_chunk)
        else:
            guessed_chunk = sorted(await self.bot.fetch_ticks(do_print=False), key=lambda x: x['trade_id'])
            return await self.find_df_enclosing_timestamp(timestamp, guessed_chunk)


        if timestamp < guessed_chunk[0]['timestamp']:
            guessed_id = (guessed_chunk[0]['trade_id'] -
                          len(guessed_chunk) * 
                          (guessed_chunk[0]['timestamp'] - timestamp) /
                           (guessed_chunk[-1]['timestamp'] - guessed_chunk[0]['timestamp']))
        else:
            guessed_id = (guessed_chunk[-1]['trade_id'] +
                          len(guessed_chunk) * 
                          (timestamp - guessed_chunk[-1]['timestamp']) /
                           (guessed_chunk[-1]['timestamp'] - guessed_chunk[0]['timestamp']))
        guessed_id = int(guessed_id - len(guessed_chunk) / 2)
        guessed_chunk = sorted(await self.bot.fetch_ticks(guessed_id, do_print=False), key=lambda x: x['trade_id'])
        print_([f"guessed_id {guessed_id} earliest ts {ts_to_date(guessed_chunk[0]['timestamp'] / 1000)[:19]} last ts {ts_to_date(guessed_chunk[-1]['timestamp'] / 1000)[:19]} target ts {ts_to_date(timestamp / 1000)[:19]}"])
        return await self.find_df_enclosing_timestamp(timestamp, guessed_chunk)

    def deduce_trade_ids(self, daily_ticks, df_for_id_matching):
        for idx in [0, -1]:
            match = daily_ticks[(daily_ticks.timestamp == df_for_id_matching.timestamp.iloc[idx]) &
                                (daily_ticks.price == df_for_id_matching.price.iloc[idx]) &
                                (daily_ticks.qty == df_for_id_matching.qty.iloc[idx])]
            if len(match) == 1:
                id_at_match = df_for_id_matching.trade_id.iloc[idx]
                return np.arange(id_at_match - match.index[0], id_at_match - match.index[0] + len(daily_ticks))
                #trade_ids = np.arange(id_at_match, id_at_match + len(daily_ticks.loc[match.index:]))
                return match, id_at_match
        raise Exception('unable to make trade ids')


    async def get_csv_gz(self, base_url, symbol, date, df_for_id_matching):
        """
        Fetches a full day of trades from the Bybit repository.
        @param symbol: Symbol to fetch.
        @param date: Day to download.
        @return: Dataframe with full day.
        """
        print_(['Fetching', symbol, date])
        url = f"{base_url}{symbol.upper()}/{symbol.upper()}{date}.csv.gz"
        df = pd.DataFrame(columns=['trade_id', 'price', 'qty', 'timestamp', 'is_buyer_maker'])
        try:
            resp = urlopen(url)
            with gzip.open(BytesIO(resp.read())) as f:
                ff = pd.read_csv(f)
                trade_ids = np.zeros(len(ff)).astype(np.int64)
                tf = pd.DataFrame({
                    'trade_id': trade_ids,
                    'price': ff.price.astype(np.float64),
                    'qty': ff['size'].astype(np.float64),
                    'timestamp': (ff.timestamp * 1000).astype(np.int64),
                    'is_buyer_maker': (ff.side == 'Sell').astype(np.int8)
                })
                tf["trade_id"] = deduce_trade_ids(tf, df_for_id_matching)
                tf.sort_values("timestamp", inplace=True)
                tf.reset_index(drop=True, inplace=True)
                del ff
                df = tf
        except Exception as e:
            print('Failed to fetch', date, e)
        return df

    async def download_ticks(self):
        """
        Searches for previously downloaded files and fills gaps in them if necessary.
        Downloads any missing data based on the specified time frame.
        @return:
        """
        if "historical_data_path" in self.config and self.config["historical_data_path"]:
            self.filepath = make_get_filepath(
                os.path.join(self.config["historical_data_path"], "historical_data",
                             self.config["exchange"], "agg_trades_futures",
                             self.config["symbol"], ""))
        else:
            self.filepath = make_get_filepath(
                os.path.join("historical_data", self.config["exchange"], "agg_trades_futures",
                             self.config["symbol"], ""))

        if self.config["exchange"] == "binance":
            self.bot = await create_binance_bot(get_dummy_settings(self.config["user"],
                                                                   self.config["exchange"],
                                                                   self.config["symbol"]))
        elif self.config["exchange"] == "bybit":
            self.bot = await create_bybit_bot(get_dummy_settings(self.config["user"],
                                                                 self.config["exchange"],
                                                                 self.config["symbol"]))
        else:
            print(self.config["exchange"], 'not found')
            return

        filenames = self.get_filenames()
        mod_files = []
        highest_id = 0
        for f in filenames:
            try:
                first_time = int(f.split("_")[2])
                last_time = int(f.split("_")[3].split(".")[0])
            except:
                first_time = sys.maxsize
                last_time = sys.maxsize
            if last_time >= self.start_time and (
                    self.end_time == -1 or (first_time <= self.end_time)) or last_time == sys.maxsize:
                print_(['Validating file', f])
                df = self.read_dataframe(os.path.join(self.filepath, f))
                missing, df, gaps = self.validate_dataframe(df)
                exists = False
                if gaps.empty:
                    first_id = df["trade_id"].iloc[0]
                else:
                    first_id = df["trade_id"].iloc[0] if df["trade_id"].iloc[0] < gaps["start"].iloc[0] else \
                        gaps["start"].iloc[0]
                if not gaps.empty and (f != filenames[-1] or str(first_id - first_id % 100000) not in f):
                    last_id = df["trade_id"].iloc[-1]
                    for i in filenames:
                        tmp_first_id = int(i.split("_")[0])
                        tmp_last_id = int(i.split("_")[1])
                        if (first_id - first_id % 100000) == tmp_first_id and (
                                (first_id - first_id % 100000 + 99999) == tmp_last_id or (
                                highest_id == tmp_first_id or highest_id == tmp_last_id) or highest_id > last_id) and first_id != 1 and i != f:
                            exists = True
                            break
                if missing and df["timestamp"].iloc[-1] > self.start_time and not exists:
                    current_time = df["timestamp"].iloc[-1]
                    for i in gaps.index:
                        print_(['Filling gaps from id', gaps["start"].iloc[i], 'to id', gaps["end"].iloc[i]])
                        current_id = gaps["start"].iloc[i]
                        while current_id < gaps["end"].iloc[i] and int(
                                datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000) - current_time > 10000:
                            loop_start = time()
                            try:
                                fetched_new_trades = await self.bot.fetch_ticks(int(current_id))
                                tf = self.transform_ticks(fetched_new_trades)
                                if tf.empty:
                                    print_(["Response empty. No new trades, exiting..."])
                                    await asyncio.sleep(max(0.0, self.fetch_delay_seconds - time() + loop_start))
                                    break
                                if current_id == tf["trade_id"].iloc[-1]:
                                    print_(["Same trade ID again. No new trades, exiting..."])
                                    await asyncio.sleep(max(0.0, self.fetch_delay_seconds - time() + loop_start))
                                    break
                                current_id = tf["trade_id"].iloc[-1]
                                df = pd.concat([df, tf])
                                df.sort_values("trade_id", inplace=True)
                                df.drop_duplicates("trade_id", inplace=True)
                                df = df[df["trade_id"] <= gaps["end"].iloc[i] - gaps["end"].iloc[i] % 100000 + 99999]
                                df.reset_index(drop=True, inplace=True)
                                current_time = df["timestamp"].iloc[-1]
                            except Exception:
                                print("Failed to fetch or transform...")
                            await asyncio.sleep(max(0.0, self.fetch_delay_seconds - time() + loop_start))
                if not df.empty:
                    if df["trade_id"].iloc[-1] > highest_id:
                        highest_id = df["trade_id"].iloc[-1]
                if not exists:
                    tf = df[df["trade_id"].mod(100000) == 0]
                    if len(tf) > 1:
                        df = df[:tf.index[-1]]
                    nf = self.save_dataframe(df, f, missing)
                    mod_files.append(nf)
                elif df["trade_id"].iloc[0] != 1:
                    os.remove(os.path.join(self.filepath, f))
                    print_(['Removed file fragment', f])

        chunk_gaps = []
        filenames = self.get_filenames()
        prev_last_id = 0
        prev_last_time = self.start_time
        for f in filenames:
            first_id = int(f.split("_")[0])
            last_id = int(f.split("_")[1])
            first_time = int(f.split("_")[2])
            last_time = int(f.split("_")[3].split(".")[0])
            if first_id - 1 != prev_last_id and f not in mod_files:
                if first_time >= prev_last_time and first_time >= self.start_time:
                    if self.end_time != -1 and self.end_time < first_time and not prev_last_time > self.end_time:
                        chunk_gaps.append((prev_last_time, self.end_time, prev_last_id, 0))
                    elif self.end_time == -1 or self.end_time > first_time:
                        chunk_gaps.append((prev_last_time, first_time, prev_last_id, first_id))
            if first_time >= self.start_time or last_time >= self.start_time:
                prev_last_id = last_id
                prev_last_time = last_time

        if len(filenames) < 1:
            chunk_gaps.append((self.start_time, self.end_time, 0, 0))
        else:
            if self.end_time == -1:
                chunk_gaps.append((prev_last_time, self.end_time, prev_last_id, 0))
            elif prev_last_time < self.end_time:
                chunk_gaps.append((prev_last_time, self.end_time, prev_last_id, 0))

        for gaps in chunk_gaps:
            start_time, end_time, start_id, end_id = gaps
            df = pd.DataFrame()

            current_id = start_id + 1
            current_time = start_time

            if self.config['exchange'] == 'binance':
                fetched_new_trades = await self.bot.fetch_ticks(1)
                tf = self.transform_ticks(fetched_new_trades)
                earliest = tf['timestamp'].iloc[0]

                if earliest > start_time:
                    start_time = earliest
                    current_time = start_time

                if end_time == -1:
                    tmp = pd.date_range(
                        start=datetime.datetime.fromtimestamp(start_time / 1000, datetime.timezone.utc).date(),
                        end=datetime.datetime.now(datetime.timezone.utc).date(), freq='M').to_pydatetime()
                else:
                    tmp = pd.date_range(
                        start=datetime.datetime.fromtimestamp(start_time / 1000, datetime.timezone.utc).date(),
                        end=datetime.datetime.fromtimestamp(end_time / 1000, datetime.timezone.utc).date(),
                        freq='M').to_pydatetime()

                months = [date.strftime("%Y-%m") for date in tmp]

                if months:
                    new_start_time = datetime.datetime.combine(tmp[-1], datetime.time.max,
                                                               datetime.timezone.utc).timestamp() * 1000 + 0.001
                else:
                    new_start_time = start_time

                if end_time == -1:
                    tmp = pd.date_range(
                        start=datetime.datetime.fromtimestamp(new_start_time / 1000, datetime.timezone.utc).date(),
                        end=datetime.datetime.now(datetime.timezone.utc).date(), freq='D').to_pydatetime()
                else:
                    tmp = pd.date_range(
                        start=datetime.datetime.fromtimestamp(new_start_time / 1000, datetime.timezone.utc).date(),
                        end=datetime.datetime.fromtimestamp(end_time / 1000, datetime.timezone.utc).date(),
                        freq='D').to_pydatetime()

                days = [date.strftime("%Y-%m-%d") for date in tmp]
                dates = months
                dates.extend(days)

                df = pd.DataFrame(columns=['trade_id', 'price', 'qty', 'timestamp', 'is_buyer_maker'])

                for date in dates:
                    if len(date.split('-')) == 2:
                        tf = self.get_zip(self.monthly_base_url, self.config['symbol'], date)
                    elif len(date.split('-')) == 3:
                        tf = self.get_zip(self.daily_base_url, self.config['symbol'], date)
                    else:
                        print("Something wrong with the date", date)
                        tf = pd.DataFrame()
                    tf = tf[tf['timestamp'] >= start_time]
                    if end_time != -1:
                        tf = tf[tf['timestamp'] <= end_time]
                    if start_id != 0:
                        tf = tf[tf['trade_id'] > start_id]
                    if end_id != 0:
                        tf = tf[tf['trade_id'] <= end_id]
                    if df.empty:
                        df = tf
                    else:
                        df = pd.concat([df, tf])
                    df.sort_values("trade_id", inplace=True)
                    df.drop_duplicates("trade_id", inplace=True)
                    df.reset_index(drop=True, inplace=True)

                    if not df.empty and (
                            (df['trade_id'].iloc[0] % 100000 == 0 and len(df) >= 100000) or df['trade_id'].iloc[
                        0] % 100000 != 0):
                        for index, row in df[df['trade_id'] % 100000 == 0].iterrows():
                            if index != 0:
                                self.save_dataframe(df[(df['trade_id'] >= row['trade_id'] - 1000000) & (
                                        df['trade_id'] < row['trade_id'])], "", True)
                                df = df[df['trade_id'] >= row['trade_id']]
                    if not df.empty:
                        start_id = df["trade_id"].iloc[0] - 1
                        start_time = df["timestamp"].iloc[0]
                        current_time = df["timestamp"].iloc[-1]
                        current_id = df["trade_id"].iloc[-1] + 1
            elif False:#self.config['exchange'] == 'bybit':

                # work in progress

                fetched_new_trades = await self.bot.fetch_ticks(1)
                tf = self.transform_ticks(fetched_new_trades)
                earliest = tf['timestamp'].iloc[0]

                if earliest > start_time:
                    start_time = earliest
                    current_time = start_time

                tmp = pd.date_range(
                    start=datetime.datetime.fromtimestamp(start_time / 1000, datetime.timezone.utc).date(),
                    end=datetime.datetime.fromtimestamp(end_time / 1000, datetime.timezone.utc).date(),
                    freq='D').to_pydatetime()

                days = [date.strftime("%Y-%m-%d") for date in tmp]
                dates = days


                if start_id == 0:
                    df_for_id_matching = await self.find_df_enclosing_timestamp(start_time)
                else:
                    df_for_id_matching = await self.fetch_ticks(start_id - 100)

                df = pd.DataFrame(columns=['trade_id', 'price', 'qty', 'timestamp', 'is_buyer_maker'])

                for date in dates:
                    if len(date.split('-')) == 3:
                        tf = self.get_csv_gz(self.daily_base_url, self.config['symbol'], date, df_for_id_matching)
                    else:
                        print("Something wrong with the date", date)
                        tf = pd.DataFrame()
                    tf = tf[tf['timestamp'] >= start_time]
                    if end_time != -1:
                        tf = tf[tf['timestamp'] <= end_time]
                    if start_id != 0:
                        tf = tf[tf['trade_id'] > start_id]
                    if end_id != 0:
                        tf = tf[tf['trade_id'] <= end_id]
                    if df.empty:
                        df = tf
                    else:
                        df = pd.concat([df, tf])
                    df.sort_values("trade_id", inplace=True)
                    df.drop_duplicates("trade_id", inplace=True)
                    df.reset_index(drop=True, inplace=True)

                    if not df.empty and (
                            (df['trade_id'].iloc[0] % 100000 == 0 and len(df) >= 100000) or df['trade_id'].iloc[
                        0] % 100000 != 0):
                        for index, row in df[df['trade_id'] % 100000 == 0].iterrows():
                            if index != 0:
                                self.save_dataframe(df[(df['trade_id'] >= row['trade_id'] - 1000000) & (
                                        df['trade_id'] < row['trade_id'])], "", True)
                                df = df[df['trade_id'] >= row['trade_id']]
                    if not df.empty:
                        start_id = df["trade_id"].iloc[0] - 1
                        start_time = df["timestamp"].iloc[0]
                        current_time = df["timestamp"].iloc[-1]
                        current_id = df["trade_id"].iloc[-1] + 1




            if start_id == 0:
                df = await self.find_time(start_time)
                current_id = df["trade_id"].iloc[-1] + 1
                current_time = df["timestamp"].iloc[-1]

            end_id = sys.maxsize if end_id == 0 else end_id - 1
            end_time = sys.maxsize if end_time == -1 else end_time

            if current_id <= end_id and current_time <= end_time and int(
                    datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000) - current_time > 10000:
                if end_time == sys.maxsize:
                    print_(['Downloading from', ts_to_date(float(current_time) / 1000), 'to current time...'])
                else:
                    print_(['Downloading from', ts_to_date(float(current_time) / 1000), 'to',
                            ts_to_date(float(end_time) / 1000)])

            while current_id <= end_id and current_time <= end_time and int(
                    datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000) - current_time > 10000:
                loop_start = time()
                fetched_new_trades = await self.bot.fetch_ticks(int(current_id))
                tf = self.transform_ticks(fetched_new_trades)
                if tf.empty:
                    print_(["Response empty. No new trades, exiting..."])
                    await asyncio.sleep(max(0.0, self.fetch_delay_seconds - time() + loop_start))
                    break
                if current_id == tf["trade_id"].iloc[-1]:
                    print_(["Same trade ID again. No new trades, exiting..."])
                    await asyncio.sleep(max(0.0, self.fetch_delay_seconds - time() + loop_start))
                    break
                df = pd.concat([df, tf])
                df.sort_values("trade_id", inplace=True)
                df.drop_duplicates("trade_id", inplace=True)
                df.reset_index(drop=True, inplace=True)
                current_time = tf["timestamp"].iloc[-1]
                current_id = tf["trade_id"].iloc[-1] + 1
                tf = df[df["trade_id"].mod(100000) == 0]
                if not tf.empty and len(df) > 1:
                    if df["trade_id"].iloc[0] % 100000 == 0 and len(tf) > 1:
                        self.save_dataframe(df[:tf.index[-1]], "", True)
                        df = df[tf.index[-1]:]
                    elif df["trade_id"].iloc[0] % 100000 != 0 and len(tf) == 1:
                        self.save_dataframe(df[:tf.index[-1]], "", True)
                        df = df[tf.index[-1]:]
                await asyncio.sleep(max(0.0, self.fetch_delay_seconds - time() + loop_start))
            if not df.empty:
                df = df[df["timestamp"] >= start_time]
                if start_id != 0 and not df.empty:
                    df = df[df["trade_id"] > start_id]
                elif end_id != sys.maxsize and not df.empty:
                    df = df[df["trade_id"] <= end_id]
                elif end_time != sys.maxsize and not df.empty:
                    df = df[df["timestamp"] <= end_time]
                if not df.empty:
                    self.save_dataframe(df, "", True)

        try:
            await self.bot.session.close()
        except:
            pass

    async def prepare_files(self, single_file: bool = False):
        """
        Takes downloaded data and prepares numpy arrays for use in backtesting.
        @param single_file: If a single array should be created ot multiple ones.
        @return:
        """
        filenames = self.get_filenames()
        start_index = 0
        for i in range(len(filenames)):
            if int(filenames[i].split("_")[2]) <= self.start_time <= int(filenames[i].split("_")[3].split(".")[0]):
                start_index = i
                break
        end_index = -1
        if self.end_time != -1:
            for i in range(len(filenames)):
                if int(filenames[i].split("_")[2]) <= self.end_time <= int(filenames[i].split("_")[3].split(".")[0]):
                    end_index = i
                    break
        filenames = filenames[start_index:] if end_index == -1 else filenames[start_index:end_index + 1]

        chunks = []
        df = pd.DataFrame()

        for f in filenames:
            if single_file:
                chunk = pd.read_csv(os.path.join(self.filepath, f),
                                    dtype={"price": np.float64, "is_buyer_maker": np.float64, "timestamp": np.float64},
                                    # "qty": np.float64},
                                    usecols=["price", "is_buyer_maker", "timestamp"])  # , "qty"])
            else:
                chunk = pd.read_csv(os.path.join(self.filepath, f),
                                    dtype={"timestamp": np.int64, "price": np.float64, "is_buyer_maker": np.int8},
                                    # "qty": np.float32},
                                    usecols=["timestamp", "price", "is_buyer_maker"])  # , "qty"])
            if self.end_time != -1:
                chunk = chunk[(chunk['timestamp'] >= self.start_time) & (chunk['timestamp'] <= self.end_time)]
            else:
                chunk = chunk[(chunk['timestamp'] >= self.start_time)]
            chunks.append(chunk)
            if len(chunks) >= 100:
                if df.empty:
                    df = pd.concat(chunks, axis=0)
                else:
                    chunks.insert(0, df)
                    df = pd.concat(chunks, axis=0)
                chunks = []
            print('\rloaded chunk of data', f, ts_to_date(float(f.split("_")[2]) / 1000), end='     ')
        print('\n')
        if chunks:
            if df.empty:
                df = pd.concat(chunks, axis=0)
            else:
                chunks.insert(0, df)
                df = pd.concat(chunks, axis=0)
            del chunks

        # df = df.groupby([(df.price != df.price.shift()).cumsum(), 'is_buyer_maker']).agg(
        #     {'qty': 'sum', 'price': 'first', 'is_buyer_maker': 'first', 'timestamp': 'first'}).reset_index(
        #     drop=True)
        # df = df.groupby([(df.price != df.price.shift()).cumsum(), 'is_buyer_maker']).agg(
        #     {'price': 'first', 'is_buyer_maker': 'first', 'timestamp': 'first'}).reset_index(drop=True)
        df = df.groupby(
            (~((df.price == df.price.shift(1)) & (df.is_buyer_maker == df.is_buyer_maker.shift(1)))).cumsum()).agg(
            {'price': 'first', 'is_buyer_maker': 'first', 'timestamp': 'first'})  # , 'qty': 'sum'})

        if single_file:
            # compressed_ticks = df[["price", "is_buyer_maker", "timestamp", "qty"]].values
            compressed_ticks = df[["price", "is_buyer_maker", "timestamp"]].values
            print_(["Saving single file with", len(df), " ticks to", self.tick_filepath, "..."])
            np.save(self.tick_filepath, compressed_ticks)
            print_(["Saved single file!"])
        else:
            print_(["Saving price file with", len(df), " ticks to", self.price_filepath, "..."])
            np.save(self.price_filepath, df[["price"]].values)
            print_(["Saved price file!"])

            print_(["Saving buyer_maker file with", len(df), " ticks to", self.buyer_maker_filepath, "..."])
            np.save(self.buyer_maker_filepath, df[["is_buyer_maker"]].values)
            print_(["Saved buyer_maker file!"])

            print_(["Saving timestamp file with", len(df), " ticks to", self.time_filepath, "..."])
            np.save(self.time_filepath, df[["timestamp"]].values)
            print_(["Saved timestamp file!"])

            # print_(["Saving qty file with", len(df), " ticks to", self.qty_filepath, "..."])
            # np.save(self.qty_filepath, compressed_ticks[:, 3])
            # print_(["Saved qty file!"])

    async def get_ticks(self, single_file: bool = False) -> (np.ndarray, np.ndarray, np.ndarray):
        """
        Function for direct use in the backtester. Checks if the numpy arrays exist and if so loads them.
        If they do not exist or if their length doesn't match, download the missing data and create them.
        @return: A tuple of three numpy arrays.
        """
        if single_file:
            if os.path.exists(self.tick_filepath):
                print_(['Loading cached tick data from', self.tick_filepath])
                tick_data = np.load(self.tick_filepath)
                return tick_data
            await self.download_ticks()
            await self.prepare_files(single_file)
            tick_data = np.load(self.tick_filepath)
            return tick_data
        else:
            if os.path.exists(self.price_filepath) and os.path.exists(self.buyer_maker_filepath) and os.path.exists(
                    self.time_filepath):  # and os.path.exists(self.qty_filepath):
                print_(['Loading cached tick data from', self.tick_filepath])
                price_data = np.load(self.price_filepath)
                buyer_maker_data = np.load(self.buyer_maker_filepath)
                time_data = np.load(self.time_filepath)
                # qty_data = np.load(self.qty_filepath)
                if len(price_data) == len(buyer_maker_data) == len(time_data):  # == len(qty_data):
                    return price_data, buyer_maker_data, time_data  # , qty_data
                else:
                    print_(['Tick data does not match, starting over...'])
                    del price_data
                    del buyer_maker_data
                    del time_data
                    # del qty_data
                    gc.collect()

            await self.download_ticks()
            await self.prepare_files(single_file)
            price_data = np.load(self.price_filepath)
            buyer_maker_data = np.load(self.buyer_maker_filepath)
            time_data = np.load(self.time_filepath)
            # qty_data = np.load(self.qty_filepath)
            return price_data, buyer_maker_data, time_data  # , qty_data

    async def get_data(self) -> (np.ndarray,):
        """
        Function for direct use in the backtester/optimizer. Checks if the numpy arrays exist and if so loads them.
        If they do not exist or if their length doesn't match, download the missing data, create them, and create
        additional data.
        @return: A tuple of numpy arrays.
        """
        cache_dirpath = os.path.join(
            self.config['caches_dirpath'],
            f"{self.config['session_name']}_n_spans_{self.config['n_spans']}",
            '')
        if not os.path.exists(cache_dirpath):
            prices, is_buyer_maker, timestamps = await self.get_ticks(False)
            prices = np.reshape(prices, prices.size)
            is_buyer_maker = np.reshape(is_buyer_maker, is_buyer_maker.size)
            timestamps = np.reshape(timestamps, timestamps.size)
            fpath = make_get_filepath(cache_dirpath)
            data = (prices, is_buyer_maker, timestamps)
            print('dumping cache...')
            for fname, arr in zip(['prices', 'is_buyer_maker', 'timestamps'], data):
                np.save(f'{fpath}{fname}.npy', arr)
            size_mb = np.sum([sys.getsizeof(d) for d in data]) / (1000 * 1000)
            print(f'dumped {size_mb:.2f} mb of data')
            del prices
            del is_buyer_maker
            del timestamps
            del data
            gc.collect()
        print('loading cached tick data')
        arrs = []
        for fname in ['prices', 'is_buyer_maker', 'timestamps']:
            arrs.append(np.load(f'{cache_dirpath}{fname}.npy'))
        return tuple(arrs)


async def main():
    parser = argparse.ArgumentParser(prog='Downloader', description='Download ticks from exchange API.')
    parser = add_argparse_args(parser)

    args = parser.parse_args()
    config = await prep_config(args)
    downloader = Downloader(config)
    await downloader.download_ticks()
    if not args.download_only:
        await downloader.prepare_files(False)
    sleep(0.1)


if __name__ == "__main__":
    asyncio.run(main())
