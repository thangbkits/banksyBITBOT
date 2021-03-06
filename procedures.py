import json
import pprint
import os
import hjson
from time import time
from pure_funcs import numpyize, denumpyize, candidate_to_live_config, ts_to_date, get_dummy_settings, calc_spans, \
    config_pretty_str


def load_live_config(live_config_path: str) -> dict:
    try:
        live_config = json.load(open(live_config_path))
        return numpyize(live_config)
    except Exception as e:
        raise Exception(f'failed to load live config {live_config_path} {e}')


def dump_live_config(config: dict, path: str):
    pretty_str = config_pretty_str(candidate_to_live_config(config))
    with open(path, 'w') as f:
        f.write(pretty_str)


async def prep_config(args) -> dict:
    try:
        bc = hjson.load(open(args.backtest_config_path, encoding='utf-8'))
    except Exception as e:
        raise Exception('failed to load backtest config', args.backtest_config_path, e)
    try:
        oc = hjson.load(open(args.optimize_config_path, encoding='utf-8'))
    except Exception as e:
        raise Exception('failed to load optimize config', args.optimize_config_path, e)
    config = {**oc, **bc}
    for key in ['exchange', 'symbol', 'user', 'start_date', 'end_date', 'starting_balance']:
        if getattr(args, key) is not None:
            config[key] = getattr(args, key)

    if config['exchange'] == 'bybit' and config['symbol'].endswith('USDT'):
        raise Exception('error: bybit linear usdt markets backtesting and optimizing not supported')

    end_date = config['end_date'] if config['end_date'] and config['end_date'] != -1 else ts_to_date(time())[:16]
    config['session_name'] = f"{config['start_date'].replace(' ', '').replace(':', '').replace('.', '')}_" \
                             f"{end_date.replace(' ', '').replace(':', '').replace('.', '')}"

    base_dirpath = os.path.join('backtests', config['exchange'], config['symbol'])
    config['caches_dirpath'] = make_get_filepath(os.path.join(base_dirpath, 'caches', ''))
    config['optimize_dirpath'] = make_get_filepath(os.path.join(base_dirpath, 'optimize', ''))
    config['plots_dirpath'] = make_get_filepath(os.path.join(base_dirpath, 'plots', ''))

    mss = config['caches_dirpath'] + 'market_specific_settings.json'
    try:
        print('fetching market_specific_settings...')
        market_specific_settings = await fetch_market_specific_settings(config['user'], config['exchange'],
                                                                        config['symbol'])
        json.dump(market_specific_settings, open(mss, 'w'), indent=4)
    except Exception as e:
        print('failed to fetch market_specific_settings', e)
        try:
            if os.path.exists(mss):
                market_specific_settings = json.load(open(mss))
            print('using cached market_specific_settings')
        except Exception as e1:
            raise Exception('failed to load cached market_specific_settings')
    config.update(market_specific_settings)

    if 'pbr_limit' in config['ranges']:
        config['ranges']['pbr_limit'][1] = min(config['ranges']['pbr_limit'][1], config['max_leverage'])
        config['ranges']['pbr_limit'][0] = min(config['ranges']['pbr_limit'][0], config['ranges']['pbr_limit'][1])


    return config


def make_get_filepath(filepath: str) -> str:
    '''
    if not is path, creates dir and subdirs for path, returns path
    '''
    dirpath = os.path.dirname(filepath) if filepath[-1] != '/' else filepath
    if not os.path.isdir(dirpath):
        os.makedirs(dirpath)
    return filepath


def load_key_secret(exchange: str, user: str) -> (str, str):
    try:
        keyfile = json.load(open('api-keys.json'))
        # Checks that the user exists, and it is for the correct exchange
        if user in keyfile and keyfile[user]["exchange"] == exchange:

            # If we need to get the `market` key:
            # market = keyfile[user]["market"]
            # print("The Market Type is " + str(market))

            keyList = [str(keyfile[user]["key"]), str(keyfile[user]["secret"])]

            return keyList
        elif user not in keyfile or keyfile[user]["exchange"] != exchange:
            print("Looks like the keys aren't configured yet, or you entered the wrong username!")
        raise Exception('API KeyFile Missing!')
    except FileNotFoundError:
        print("File Not Found!")
        raise Exception('API KeyFile Missing!')


def print_(args, r=False, n=False):
    line = ts_to_date(time())[:19] + '  '
    str_args = '{} ' * len(args)
    line += str_args.format(*args)
    if n:
        print('\n' + line, end=' ')
    elif r:
        print('\r' + line, end=' ')
    else:
        print(line)
    return line


async def fetch_market_specific_settings(user: str, exchange: str, symbol: str):
    tmp_live_settings = get_dummy_settings(user, exchange, symbol)
    settings_from_exchange = {}
    if exchange == 'binance':
        bot = await create_binance_bot(tmp_live_settings)
        settings_from_exchange['maker_fee'] = 0.0002
        settings_from_exchange['taker_fee'] = 0.0004
        settings_from_exchange['exchange'] = 'binance'
    elif exchange == 'bybit':
        bot = await create_bybit_bot(tmp_live_settings)
        settings_from_exchange['maker_fee'] = -0.00025
        settings_from_exchange['taker_fee'] = 0.00075
        settings_from_exchange['exchange'] = 'bybit'
    else:
        raise Exception(f'unknown exchange {exchange}')
    await bot.session.close()
    if 'inverse' in bot.market_type:
        settings_from_exchange['inverse'] = True
    elif 'linear' in bot.market_type:
        settings_from_exchange['inverse'] = False
    else:
        raise Exception('unknown market type')
    for key in ['max_leverage', 'min_qty', 'min_cost', 'qty_step', 'price_step', 'max_leverage',
                'c_mult', 'hedge_mode']:
        settings_from_exchange[key] = getattr(bot, key)
    return settings_from_exchange


async def create_binance_bot(config: dict):
    from binance import BinanceBot
    bot = BinanceBot(config)
    await bot._init()
    return bot


async def create_binance_bot_spot(config: dict):
    from binance_spot import BinanceBotSpot
    bot = BinanceBotSpot(config)
    await bot._init()
    return bot


async def create_bybit_bot(config: dict):
    from bybit import Bybit
    bot = Bybit(config)
    await bot._init()
    return bot


def add_argparse_args(parser):
    parser.add_argument('--nojit', help='disable numba', action='store_true')
    parser.add_argument('-b', '--backtest_config', type=str, required=False, dest='backtest_config_path',
                        default='configs/backtest/default.hjson', help='backtest config hjson file')
    parser.add_argument('-o', '--optimize_config', type=str, required=False, dest='optimize_config_path',
                        default='configs/optimize/default.hjson', help='optimize config hjson file')
    parser.add_argument('-d', '--download-only', help='download only, do not dump ticks caches', action='store_true')
    parser.add_argument('-s', '--symbol', type=str, required=False, dest='symbol',
                        default=None, help='specify symbol, overriding symbol from backtest config')
    parser.add_argument('-e', '--exchange', type=str, required=False, dest='exchange',
                        default=None, help='specify exchange (binance/bybit), overriding exchange from backtest config')
    parser.add_argument('-u', '--user', type=str, required=False, dest='user',
                        default=None,
                        help='specify user, a.k.a. account_name, overriding user from backtest config')
    parser.add_argument('--start_date', type=str, required=False, dest='start_date',
                        default=None,
                        help='specify start date, overriding value from backtest config')
    parser.add_argument('--end_date', type=str, required=False, dest='end_date',
                        default=None,
                        help='specify end date, overriding value from backtest config')
    parser.add_argument('--starting_balance', type=float, required=False, dest='starting_balance',
                        default=None,
                        help='specify starting_balance, overriding value from backtest config')
    return parser
