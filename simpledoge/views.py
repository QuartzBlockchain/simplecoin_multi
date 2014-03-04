import calendar
import time
import yaml
import datetime

from itsdangerous import TimedSerializer
from flask import (current_app, request, render_template, Blueprint, abort,
                   jsonify, g, session)
from sqlalchemy.sql import func

from .models import (Transaction, OneMinuteShare, Block, Share, Payout,
                     last_block_share_id, last_block_time, Blob)
from . import db, root, cache


main = Blueprint('main', __name__)


@main.route("/")
def home():
    news = yaml.load(open(root + '/static/yaml/news.yaml'))
    alerts = yaml.load(open(root + '/static/yaml/alerts.yaml'))
    return render_template('home.html', news=news, alerts=alerts)


@main.route("/news")
def news():
    news = yaml.load(open(root + '/static/yaml/news.yaml'))
    return render_template('news.html', news=news)


@main.route("/pool_stats")
def pool_stats():
    current_block = db.session.query(Blob).filter_by(key="block").first()
    current_block.data['reward'] = int(current_block.data['reward'])
    blocks = db.session.query(Block).order_by(Block.height.desc()).limit(10)
    return render_template('pool_stats.html', blocks=blocks,
                           current_block=current_block)


@main.route("/get_payouts", methods=['POST'])
def get_payouts():
    """ Used by remote procedure call to retrieve a list of transactions to
    be processed. Transaction information is signed for safety. """
    s = TimedSerializer(current_app.config['rpc_signature'])
    s.loads(request.data)

    payouts = (Payout.query.filter_by(transaction_id=None).
               join(Payout.transaction, aliased=True).filter_by(confirmed=True))
    struct = [(p.user, p.amount, p.id)
              for p in payouts]
    return s.dumps(struct)


@main.route("/confirm_payouts", methods=['POST'])
def confirm_transactions():
    """ Used as a response from an rpc payout system. This will either reset
    the sent status of a list of transactions upon failure on the remote side,
    or create a new CoinTransaction object and link it to the transactions to
    signify that the transaction has been processed. Both request and response
    are signed. """
    s = TimedSerializer(current_app.config['rpc_signature'])
    data = s.loads(request.data)

    # basic checking of input
    try:
        assert len(data['coin_txid']) == 64
        assert isinstance(data['pids'], list)
        for id in data['pids']:
            assert isinstance(id, int)
    except AssertionError:
        abort(400)

    coin_trans = Transaction.create(data['coin_txid'])
    db.session.flush()
    Payout.query.filter(Payout.id.in_(data['pids'])).update(
        {Payout.transaction_id: coin_trans.txid}, synchronize_session=False)
    db.session.commit()
    return s.dumps(True)


@main.before_request
def add_pool_stats():
    g.pool_stats = get_frontpage_data()

    additional_seconds = (datetime.datetime.utcnow() - g.pool_stats[2]).total_seconds()
    ratio = g.pool_stats[0]/g.pool_stats[1]
    additional_shares = ratio * additional_seconds
    g.pool_stats[0] += additional_shares
    g.pool_stats[1] += additional_seconds
    g.current_difficulty = db.session.query(Blob).filter_by(key="block").first().data['difficulty']


@cache.cached(timeout=60, key_prefix='get_total_n1')
def get_frontpage_data():

    # A bit inefficient, but oh well... Make it better later...
    last_share_id = last_block_share_id()
    last_found_at = last_block_time()
    dt = datetime.datetime.utcnow()
    ten_min = (OneMinuteShare.query.filter_by(user='pool')
                .order_by(OneMinuteShare.minute.desc())
                .limit(10))
    ten_min = sum([min.shares for min in ten_min])
    shares = db.session.query(func.sum(Share.shares)).filter(Share.id > last_share_id).scalar() or 0
    last_dt = (datetime.datetime.utcnow() - last_found_at).total_seconds()
    return [shares, last_dt, dt, ten_min]


@cache.memoize(timeout=60)
def last_10_shares(user):
    ten_ago = (datetime.datetime.utcnow() - datetime.timedelta(minutes=10)).replace(second=0)
    minutes = (OneMinuteShare.query.
               filter_by(user=user).filter(OneMinuteShare.minute >= ten_ago).
               order_by(OneMinuteShare.minute.desc()).
               limit(10))
    if minutes:
        return sum([min.shares for min in minutes])
    return 0


@cache.memoize(timeout=60)
def total_earned(user):
    return (db.session.query(func.sum(Payout.amount)).
            filter_by(user=user).scalar() or 0)

@cache.memoize(timeout=60)
def total_paid(user):
    total_p = (Payout.query.filter_by(user=user).
              join(Payout.transaction, aliased=True).
              filter_by(confirmed=True))
    return sum([tx.amount for tx in total_p])


@main.route("/charity")
def charity_view():
    charities = []
    for info in current_app.config['aliases']:
        info['hashes_per_min'] = ((2 ** 16) * last_10_shares(info['address'])) / 600
        info['total_paid'] = total_paid(info['address'])
        charities.append(info)
    return render_template('charity.html', charities=charities)


@main.route("/<address>")
def user_dashboard(address=None):
    if len(address) != 34:
        abort(404)
    earned = total_earned(address)
    total_paid = (Payout.query.filter_by(user=address).
                  join(Payout.transaction, aliased=True).
                  filter_by(confirmed=True))
    total_paid = sum([tx.amount for tx in total_paid])
    balance = earned - total_paid
    unconfirmed_balance = (Payout.query.filter_by(user=address).
                           join(Payout.block, aliased=True).
                           filter_by(mature=False))
    unconfirmed_balance = sum([payout.amount for payout in unconfirmed_balance])
    balance -= unconfirmed_balance

    payouts = db.session.query(Payout).filter_by(user=address).limit(20)
    last_share_id = last_block_share_id()
    user_shares = (db.session.query(func.sum(Share.shares)).
                   filter(Share.id > last_share_id, Share.user == address).
                   scalar() or 0)

    # reorganize/create the recently viewed
    recent = session.get('recent_users', [])
    if address in recent:
        recent.remove(address)
    recent.insert(0, address)
    session['recent_users'] = recent[:10]

    return render_template('user_stats.html',
                           username=address,
                           user_shares=user_shares,
                           payouts=payouts,
                           round_reward=250000,
                           total_earned=earned,
                           total_paid=total_paid,
                           balance=balance,
                           unconfirmed_balance=unconfirmed_balance)


@main.route("/<address>/stats")
def address_stats(address=None):
    minutes = (db.session.query(OneMinuteShare).
               filter_by(user=address).order_by(OneMinuteShare.minute.desc()).
               limit(1440))
    data = {calendar.timegm(minute.minute.utctimetuple()): minute.shares
            for minute in minutes}
    day_ago = ((int(time.time()) - (60 * 60 * 24)) // 60) * 60
    out = [(i, data.get(i) or 0)
           for i in xrange(day_ago, day_ago + (1440 * 60), 60)]

    return jsonify(points=out, length=len(out))


@main.route("/guides/<guide>")
def guides(guide):
    return render_template(guide + ".html")
