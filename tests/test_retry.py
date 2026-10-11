"""v0.4.1: a restock is when the shop is slowest. Transient failures before the order request was sent
are retried; once it was sent nothing is ever sent again. A busy site must not discard the login."""
import contextlib
import json
import time
import unittest
from unittest.mock import patch
import test_app  # noqa: F401
from test_checkout import Merchant
from test_accounts import COOKIE, LOGIN
from test_purchase import PurchaseTests, ORDER, BERO_CFG
from store import db
import accounts
import checkout
import purchases
from merchants import bero


class Flaky:
    """The recorded shop, but request number `fail_at` (0-based, GET and POST together) raises."""
    def __init__(self, fail_at=None, error=TimeoutError, **kwargs):
        self.inner = Merchant(**kwargs)
        self.count = -1
        self.fail_at, self.error = fail_at, error
        self.timeout = 8

    def request(self, path, **kwargs):
        self.count += 1
        if self.count == self.fail_at:
            raise self.error('boom')
        return self.inner.request(path, **kwargs)


class RetryTests(PurchaseTests):
    """Reuses the armed-task fixture of PurchaseTests (its own tests are skipped below)."""

    def run_job(self, shops, notified=None):
        shops = list(shops)

        @contextlib.contextmanager
        def session():
            yield shops.pop(0)

        with patch('merchants.bero.accounts.checkout_session', session), patch('checkout.time.sleep') as sleep:
            checkout.enqueue(self.id, 'purchase')
            checkout.run_next(notify=notified)
        return sleep, shops

    def result(self):
        return checkout.latest(self.id)['result']

    def test_timeouts_before_the_order_are_retried_until_it_goes_through(self):
        # attempt 1: balance page times out; attempt 2: fails after the attempt was reserved (terms toggle);
        # attempt 3 succeeds. Exactly one order request is ever sent.
        toggle_call = 7  # 0 balance, 1 store page, 2-6 order calls, 7 = terms toggle, 8 = final order
        shops = [Flaky(fail_at=0, stock=True), Flaky(fail_at=toggle_call, stock=True), Flaky(stock=True)]
        sleep, left = self.run_job(shops)
        self.assertEqual(left, [])
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(self.result()['status'], 'purchased')
        self.assertIn('第 3 次尝试', self.result()['message'])
        self.assertEqual(shops[2].inner.methods(), ORDER)
        self.assertEqual(shops[1].inner.methods().count('jumpToStep[1500]'), 0)
        self.assertEqual([a['state'] for a in self.attempts()], ['success'])
        self.assertFalse(purchases.is_armed(purchases.policy_for(self.id)))

    def test_site_errors_503_are_retried_too(self):
        shops = [Flaky(fail_at=0, error=lambda m: accounts.LoginIssue('error', m), stock=True), Flaky(stock=True)]
        sleep, _ = self.run_job(shops)
        self.assertEqual(self.result()['status'], 'purchased')

    def test_gives_up_after_the_limit_and_stays_armed_with_nothing_sent(self):
        shops = [Flaky(fail_at=0, stock=True) for _ in range(checkout.RETRY_TRIES + 2)]
        sleep, left = self.run_job(shops)
        self.assertEqual(len(left), 2)
        self.assertEqual(sleep.call_count, checkout.RETRY_TRIES - 1)
        self.assertEqual(self.result()['status'], 'error')
        self.assertFalse(self.result()['order_submitted'])
        self.assertEqual(self.attempts(), [])
        self.assertTrue(purchases.is_armed(purchases.policy_for(self.id)))

    def test_a_sent_order_is_never_retried(self):
        # the final request times out: result is uncertain and there is only one session
        # request 8 is jumpToStep(1500). It times out and the balance did not change: uncertain, never resent.
        shop = Flaky(fail_at=8, stock=True, balances=('44,10',))
        sleep, left = self.run_job([shop, Flaky(stock=True)])
        self.assertEqual(len(left), 1)  # second shop untouched
        sleep.assert_not_called()
        self.assertEqual(self.result()['status'], 'uncertain')
        self.assertEqual([a['state'] for a in self.attempts()], ['uncertain'])
        self.assertFalse(purchases.is_armed(purchases.policy_for(self.id)))

    def test_final_request_timeout_with_balance_deducted_counts_as_purchased(self):
        shop = Flaky(fail_at=8, stock=True)  # balance goes 44,10 -> 0,00
        sleep, _ = self.run_job([shop, Flaky(stock=True)])
        sleep.assert_not_called()
        self.assertEqual(self.result()['status'], 'purchased')
        self.assertEqual(shop.inner.methods().count('jumpToStep[1500]'), 0)  # the request never reached the shop

    def test_real_problems_are_not_retried(self):
        shops = [Flaky(fail_at=0, error=bero.PreflightError, stock=True), Flaky(stock=True)]
        sleep, left = self.run_job(shops)
        sleep.assert_not_called()
        self.assertEqual(len(left), 1)
        self.assertEqual(self.result()['status'], 'error')

    def test_no_retry_after_disarm_or_window(self):
        def disarm_then_fail(*a, **k):
            purchases.disarm(self.id)

        shops = [Flaky(fail_at=0, stock=True), Flaky(stock=True)]
        with patch('checkout.time.sleep', side_effect=disarm_then_fail):
            shops_iter = list(shops)

            @contextlib.contextmanager
            def session():
                yield shops_iter.pop(0)

            with patch('merchants.bero.accounts.checkout_session', session):
                checkout.enqueue(self.id, 'purchase')
                checkout.run_next()
        self.assertEqual(len(shops_iter), 1)  # the retry noticed auto-pay was switched off
        self.assertEqual(self.attempts(), [])

    def test_timeout_is_longer_during_a_purchase(self):
        shop = Flaky(stock=True)
        bero.purchase(shop, BERO_CFG, self.policy, self.id)
        self.assertEqual(shop.timeout, bero.PURCHASE_TIMEOUT)


class SessionSurvivesBusySiteTests(unittest.TestCase):
    def setUp(self):
        with db() as c:
            c.execute('DELETE FROM merchant_accounts')
        accounts.save_credentials({'email': 'account@example.com', 'password': 'private-password', 'auto_relogin': True})
        with db() as c:
            c.execute("UPDATE merchant_accounts SET session=?,status='authenticated',last_verified=?",
                      (accounts.encode([COOKIE]), time.time()))

    def use(self, error):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.request.return_value = '<a href="/logout">x</a>'
        client.snapshot.return_value = [COOKIE]
        with patch('accounts.BeroSession', return_value=client):
            with self.assertRaises(accounts.LoginIssue):
                with accounts.checkout_session():
                    raise error

    def test_busy_site_keeps_session_and_login_status(self):
        self.use(accounts.LoginIssue('error', 'BERO 登录页面返回异常，请稍后再验证'))
        status = accounts.public_status()
        self.assertTrue(status['session_saved'])
        self.assertEqual(status['status'], 'authenticated')
        self.assertTrue(accounts.usable())

    def test_real_login_problems_still_discard_the_session(self):
        for kind in ('expired', 'blocked', 'login_failed', 'manual_required'):
            with db() as c:
                c.execute("UPDATE merchant_accounts SET session=?,status='authenticated'", (accounts.encode([COOKIE]),))
            self.use(accounts.LoginIssue(kind, 'x'))
            self.assertFalse(accounts.public_status()['session_saved'], kind)
            self.assertFalse(accounts.usable(), kind)

    def test_queueing_works_while_the_last_background_check_failed(self):
        import adapters
        with db() as c:
            for t in ('tasks', 'purchase_policies', 'checkout_preflights', 'hosts'):
                c.execute('DELETE FROM ' + t)
            tid = c.execute('INSERT INTO tasks(config,created,enabled) VALUES (?,?,1)',
                            (json.dumps(adapters.validate(adapters.PRESETS['bero'])), time.time())).lastrowid
            c.execute("UPDATE merchant_accounts SET status='error',message='登录验证失败，请检查网络后再试'")
        purchases.save_policy(tid, {'max_total': '44.10', 'payment': 'balance'})
        self.assertFalse(bero.account_ready())  # arming stays strict
        self.assertTrue(bero.account_usable())
        checkout.enqueue(tid)  # a dry run can be queued
        with db() as c:
            c.execute("UPDATE merchant_accounts SET session=NULL")
        with db() as c:
            c.execute('DELETE FROM checkout_preflights')
        with self.assertRaises(ValueError):
            checkout.enqueue(tid)


# Do not re-run PurchaseTests' own scenarios through the subclass.
for _name in [n for n in dir(PurchaseTests) if n.startswith('test_')]:
    setattr(RetryTests, _name, None)

if __name__ == '__main__':
    unittest.main()
