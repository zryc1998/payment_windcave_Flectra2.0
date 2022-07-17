# coding: utf-8

from collections import namedtuple
from datetime import datetime
from hashlib import sha256
import hmac
import json
import math
import logging
import requests
import pprint
import base64
import hashlib
import time
from requests.exceptions import HTTPError
from werkzeug import urls
import werkzeug
import os

from flectra import api, fields, models, _, service
from flectra.http import request
from flectra.tools.float_utils import float_round
from flectra.tools import consteq
from flectra.exceptions import ValidationError

from flectra.addons.payment_windcave.controllers.main import WindcaveController
from .windcave_helper import WindcaveHelper

_logger = logging.getLogger(__name__)
_helper = WindcaveHelper()

class PaymentAcquirerWindcave(models.Model):
    _inherit = 'payment.acquirer'

    provider = fields.Selection(selection_add=[
        ('windcave', 'Windcave')
    ], ondelete={'windcave': 'set default'})

    windcave_api_url = fields.Char(groups='base.group_user')
    windcave_api_user = fields.Char(groups='base.group_user')
    windcave_api_key = fields.Char(groups='base.group_user')
    windcave_payment_methods = fields.Many2many('windcave.method', string='Payment Methods')

    _retry_limit = 5

    def windcave_form_generate_values(self, tx_values):
        self.ensure_one()
        _logger.info('PaymentAcquirerWindcave.windcave_form_generate_values()')

        tx_values['windcave_redirect_url'] = self.start_session(tx_values)
        
        payment_transaction = request.env['payment.transaction'].sudo().search([('reference','=',tx_values['reference'])])
        payment_transaction.write({'windcave_payment_exists': tx_values['windcave_payment_exists'], 
                                    'windcave_result': tx_values['windcave_result'],
                                    'windcave_amount_refunded': tx_values['windcave_amount_refunded'],
                                    'windcave_pending_transactions': tx_values['windcave_pending_transactions'],
                                    'windcave_fully_refunded': tx_values['windcave_fully_refunded']})
        
        return tx_values
    
    def  windcave_get_form_action_url(self):
        _logger.info('WINDCAVE GET_FORM_ACTION_URL')
        self.ensure_one()
        return urls.url_join(self.get_base_url(), '/payment/windcave/redirect_handler')

    def start_session(self, data):
        _logger.info('PaymentAcquirerWindcave._start_session()')

        data['windcave_payment_exists'] = False
        data['windcave_result'] = 'pending'
        data['windcave_amount_refunded'] = 0
        data['windcave_pending_transactions'] = 0
        data['windcave_fully_refunded'] = False

        order_id = data['reference'].split('-',1)[0]

        if self.env['windcave.transaction'].check_transaction_set(order_id, 'purchase') or self.env['windcave.transaction'].check_transaction_set(order_id, 'auth'):
            _logger.info('Payment already made.')
            data['windcave_payment_exists'] = True
            data['windcave_result'] = 'approved'
            return False

        session = self.env['windcave.session'].search([('order_id', '=', order_id)], order='write_date desc')
        if len(session) > 1:
            session = session[0]
        if session.session_state in ['pending', 'processing']:
            _logger.info('Session already exists. Session Id: %s. Checking amount and currency.', session.session_id)
            if math.isclose(float(session.amount), data['amount']) and session.currency == data['currency'].name:
                _logger.info('Amount and currency match order. Redirecting to HPP.')
                return session.session_url
            _logger.info('Amount and currency do not match order. Unlinking old session from order and creating new session.')
            session.update({'order_id':'unlinked'})

        xid = _helper.get_xid(self.get_base_url(), data['reference'], 'session')
        result = self._create_session(xid, data)

        if result == True:
            transaction = self.env['windcave.transaction'].search([('reference', '=', data['reference'])])
            if transaction.transaction_type in ['purchase', 'auth']:
                _logger.info("Payment successful.")
                #http_response_code(200)
                data['windcave_payment_exists'] = True
                data['windcave_result'] = 'approved'
            else:
                _logger.info("Payment unsuccessful.")
                #http_response_code(200)
                data['windcave_payment_exists'] = True
                data['windcave_result'] = 'declined'
            return False

        elif result == False:
            _logger.info("Payment error.")
            data['windcave_payment_exists'] = True
            data['windcave_result'] = 'error'
            return False
        elif "redirect" in  result.values():
            _logger.info("Redirect requested")
            return result['url']

    def _create_session(self, xid, session_data, retries = 0):
        _logger.info('PaymentAcquirerWindcave._create_session()')

        base_url = self.get_base_url()

        if self.capture_manually:
            transaction_type = 'auth'
        else:
            transaction_type = 'purchase'

        data = {
            'type': transaction_type,
            'amount': session_data['amount'],
            'currency': session_data['currency'].name,
            'methods': [method.code for method in self.windcave_payment_methods],
            'merchantReference':session_data['reference'],
            'language':session_data['partner_lang'],
            'callbackUrls':{
                'approved':urls.url_join(base_url, WindcaveController._callback_url), 
                'declined':urls.url_join(base_url, WindcaveController._callback_url), 
                'canceled':urls.url_join(base_url, WindcaveController._callback_url)
            },
            'notificationUrl':urls.url_join(base_url, WindcaveController._notification_url),
            'storeCustomer':False,
            'customer':{
                'firstName':session_data['partner_first_name'],
                'lastName':session_data['partner_last_name'],
                'phoneNumber':session_data['partner_phone'],
                'email':session_data['partner_email'],
                'billing':{
                    'name':session_data['billing_partner_name'],
                    'address1':session_data['billing_partner_address'],
                    'address2':'',
                    'city':session_data['billing_partner_city'],
                    'countryCode':session_data['billing_partner_country'].code,
                    'postalCode':session_data['billing_partner_zip'],
                    'state':session_data['billing_partner_state'].code
                },
                'shipping':{
                    'name':session_data['partner_first_name'],
                    'address1':session_data['partner_address'],
                    'address2':'',
                    'city':session_data['partner_city'],
                    'countryCode':session_data['partner_country'].code,
                    'postalCode':session_data['partner_zip'],
                    'state':session_data['partner_state'].code
                }
            }
        }

        transaction = self.env['payment.transaction'].sudo().search([('reference', '=', session_data['reference'])])

        if transaction.type == 'form_save':
            data['storeCard'] = True
            data['storedCardIndicator'] = 'credentialonfileinitial'

        response = self._do_request(data, xid)
        response_data = response.json()

        _logger.info("Response. Status Code: %s. Body: %s", response.status_code, response_data)

        if response.status_code == 200:
            transaction_data = _helper.get_transaction(response_data)
            approved = transaction_data.authorised
            session = self.env['windcave.session'].search([('reference', '=', session_data['reference'])])
            if approved:
                _logger.info("Transaction approved. Adding transaction record")
                self.env['windcave.transaction'].add_transaction(response_data, 'approved', session.reference)
                if (response_data['storeCard'] and transaction_data['card'] is not None): #and customer wants to store
                        self.env['payment.token'].sudo().create({
                        'acquirer_id' : self.acquirer_id.id,
                        'partner_id' : session.customer_id,
                        'acquirer_ref' : transaction_data['card']['id'],
                        'windcave_card_id' : transaction_data['card']['id'],
                        'windcave_card_number' : transaction_data['card']['cardNumber'],
                        'windcave_card_expiration' : transaction_data['card']['dateExpiryMonth'] + '/' + transaction_data['card']['dateExpiryYear']
                    })
                _logger.info("Setting session state: approved")
                session.update({'session_state':'approved'})
            else:
                _logger.info("Transaction declined. Adding transaction record")
                self.env['windcave.transaction'].add_transaction(response_data, 'declined', session.reference)
                _logger.info("Setting session state: declined")
                session.update({'session_state':'declined'})
            return True
        elif response.status_code == 202:
            _logger.info("Session created. Adding session record.")
            session_url = _helper.findHPPUrl(response_data['links'])
            WindcaveSession = self.env['windcave.session']
            WindcaveSession.create({
                'session_id' : response_data['id'],
                'session_state' : 'pending',
                'reference' : session_data['reference'],
                'order_id' : session_data['reference'].split('-',1)[0],
                'session_url' : session_url,
                'customer_id' : session_data['partner'].id,
                'amount' : session_data['amount'],
                'currency' : session_data['currency'].name
                })
            return {
                'result':'redirect',
                'url': session_url
            }
        elif 400 <= response.status_code < 500:
            _logger.error("Request failed. Details: %s", _helper.parse_errors(response_data['errors']))
            return False
        elif 500 <= response.status_code < 500:
            if retries < self._retry_limit:
                time.sleep(5)
                _logger.info("Request failed. Retrying")
                return self._create_session(xid, session_data, retries + 1)
            else:
                _logger.error("Request failed. Gave up after %s retries.", retries)
                return False
        else:
            if retries < self._retry_limit:
                time.sleep(5)
                _logger.info("Request failed. Retrying")
                return self._create_session(xid, session_data, retries + 1)
            else:
                _logger.error("Request failed. Gave up after %s retries.", retries)
                return False

    def _do_request(self, data=None, xid=None):
        self.ensure_one()
        _logger.info('PaymentAcquirerWindcave.do_windcave_request()')
        auth_to_encode = (self.windcave_api_user + ":" + self.windcave_api_key).encode('ascii')
        authorization = base64.b64encode(auth_to_encode).decode('ascii')
        headers = {
            "Content-Type" : "application/json; charset=utf-8",
            "Authorization" : "Basic " + authorization,
            "X-ID" : xid,
            "X-Client-Version" : "Flectra: " + service.common.exp_version()['server_version'] + ", Extension: " + self.env.ref('base.module_payment_windcave').installed_version
        }
        data_str = json.dumps(data)
        payload = json.loads(data_str)
        response = requests.post( self.windcave_api_url + '/v1/sessions', json=payload, headers=headers)
        return response

    def _get_feature_support(self):
        res = super(PaymentAcquirerWindcave, self)._get_feature_support()
        res['authorize'].append('windcave')
        res['tokenize'].append('windcave')
        return res

class WindcaveMethod(models.Model):
    _name = 'windcave.method'
    _description = 'Windcave Payment Method'

    name = fields.Char(string='Name')
    code = fields.Char(string='Code')

class PaymentTransactionWindcave(models.Model):
    _inherit = 'payment.transaction'

    windcave_payment_exists = fields.Boolean('Payment Exists')
    windcave_result = fields.Char('Result')
    windcave_amount_refunded = fields.Monetary('Amount Refunded')
    windcave_fully_refunded = fields.Boolean('Fully Refunded')
    windcave_pending_transactions = fields.Integer('Pending Transactions')

    _retry_limit = 5

    @api.model
    def _windcave_form_get_tx_from_data(self, data):
        _logger.info('PaymentTransactionWindcave._windcave_form_get_tx_from_data')

        if data.get('sessionId') != None:
            session = self.env['windcave.session'].search([('session_id', '=', data['sessionId'])])
            tx = self.env['payment.transaction'].search([('reference', '=', session.reference)])
        elif data.get('id') != None:
            windcaveTransaction = self.env['windcave.transaction'].search([('transaction_id', '=', data['id'])])
            tx = self.env['payment.transaction'].search([('reference', '=', windcaveTransaction.reference)])
        
        return tx
    
    def _windcave_form_validate(self, data):
        _logger.info('PaymentTransactionWindcave._windcave_form_validate')

        if data.get('type') == 'callback':
            return self._callback(data)
        elif data.get('sessionId') != None:
            self._sessionNotification(data.get('sessionId'))
        elif data.get('id') != None:
            self._transactionNotification(data.get('id'))

    def windcave_s2s_capture_transaction(self, **kwargs):
        _logger.info("PaymentTransactionWindcave _windcave_s2s_capture_transaction")

        order_id = self.reference.split('-',1)[0]

        auth_transaction = self.env['windcave.transaction'].search([('reference', '=', self.reference), ('transaction_type', '=', 'auth'), ('status', '=', 'approved')])

        if auth_transaction.reference == False:
           _logger.error("Complete failed: Auth transaction not found.")
           raise ValidationError(_('Complete failed: Auth transaction not found.'))

        if auth_transaction.get_pending_count(auth_transaction.reference) > 0:
            _logger.info('Complete failed: Pending transactions.')
            raise ValidationError(_('Complete failed: Pending transactions.'))
        
        if self.env['windcave.transaction'].check_transaction_set(order_id, 'complete'):
            _logger.info('Complete already made. Do not continue with transaction.')
            self._set_transaction_done()
            return
        
        if self.env['windcave.transaction'].check_transaction_set(order_id, 'void'):
            _logger.info('Void already made. Do not continue with transaction.')
            self._set_transaction_cancel()
            return
        
        auth_transaction.do_complete()
    
    def windcave_s2s_void_transaction(self, **kwargs):
        _logger.info("PaymentTransactionWindcave _windcave_s2s_capture_transaction")

        order_id = self.reference.split('-',1)[0]

        auth_transaction = self.env['windcave.transaction'].search([('reference', '=', self.reference), ('transaction_type', '=', 'auth'), ('status', '=', 'approved')])

        if auth_transaction.reference == False:
           _logger.error("Void failed: Auth transaction not found.")
           raise ValidationError(_('Void failed: Auth transaction not found.'))
        
        if auth_transaction.get_pending_count(auth_transaction.reference) > 0:
            _logger.info('Void failed: Pending transactions.')
            raise ValidationError(_('Void failed: Pending transactions.'))

        if auth_transaction.check_transaction_set(order_id, 'complete'):
            _logger.info('Complete already made. Do not continue with transaction.')
            self._set_transaction_done()
            return
        
        if auth_transaction.check_transaction_set(order_id, 'void'):
            _logger.info('Void already made. Do not continue with transaction.')
            self._set_transaction_cancel()
            return
        
        auth_transaction.do_void()

    def windcave_s2s_do_transaction(self, **data):
        self.ensure_one()

        _logger.info('PaymentAcquirerWindcave.windcave_s2s_do_transaction()')

        order_id = self.reference.split('-',1)[0]

        if self.acquirer_id.capture_manually:
            transaction_type = 'auth'
        else:
            transaction_type = 'purchase'
        
        if self.env['windcave.transaction'].check_transaction_set(order_id, 'purchase') or self.env['windcave.transaction'].check_transaction_set(order_id, 'auth'):
            _logger.info('Payment already made. Do not continue with transaction.')
            self._set_transaction_done()
            return
        
        session = self.env['windcave.session'].search([('order_id', '=', order_id)], order='write_date desc')
        if len(session) > 1:
            session = session[0]
        if session.session_state in ['pending', 'processing']:
            _logger.info('Session already exists. Session Id: %s. Checking amount and currency.', session.session_id)
            if math.isclose(float(session.amount), self.amount) and session.currency == self.currency_id.name:
                _logger.info('Amount and currency match order. Vaild session exists, do not continue with transaction.')
                return
            _logger.info('Amount and currency do not match order. Unlinking old session from order.')
            session.update({'order_id':'unlinked'})

        self.env['windcave.transaction'].do_rebill(self.reference, transaction_type, self.amount, self.currency_id.name, self.payment_token_id.windcave_card_id)
        
    def windcave_s2s_do_refund(self, **data):
        _logger.info('PaymentAcquirerWindcave.windcave_s2s_do_refund()')

        transaction = self.env['windcave.transaction'].search([('reference', '=', self.reference), ('transaction_type', 'in', ['complete', 'purchase']), ('status', '=', 'approved')])

        if transaction.reference == False:
           _logger.error("Refund failed: Complete or Purchase transaction not found.")
           raise ValidationError(_('Refund failed: Complete or Purchase transaction not found.'))
        
        transaction.do_refund()

    def action_show_refund_wizard(self, **data):
        _logger.info('PaymentAcquirerWindcave.action_show_refund_wizard()')
        
        transaction = self.env['windcave.transaction'].search([('reference', '=', self.reference), ('transaction_type', 'in', ['complete', 'purchase']), ('status', '=', 'approved')])

        if transaction.reference == False:
            _logger.error("Refund failed: Complete or Purchase transaction not found.")
            raise ValidationError(_('Refund failed: Complete or Purchase transaction not found.'))

        return {
            'type': 'ir.actions.act_window',
            'name': _('Windcave Refund'),
            'res_model': 'windcave.transaction.refund.wizard',
            'view_type': 'form',
            'view_mode': 'form',
            'target': 'new',
            'view_id': self.env.ref('payment_windcave.windcave_refund_wizard_form').id,
            'context': {'default_refund_amount': transaction.get_balance(), 'default_currency_id': self.currency_id.id, 'default_reference': self.reference}
        }
    
    def action_query_transaction(self, **data):
        self.ensure_one()

        _logger.info('PaymentAcquirerWindcave.action_query_transaction()')

        transaction = self.env['windcave.transaction'].search([('reference', '=', self.reference),  ('status', '=', 'pending')])

        if transaction.reference == False:
            _logger.error("Query failed: Pending transaction not found.")
            raise ValidationError(_('Query failed: Pending transaction not found.'))
        
        transaction.do_query()

    def _callback(self, data):
        retries = data.get('retries')
        if retries is None:
            retries = 0
        else:
            retries = int(retries)

        session_id = data.get('sessionId')
        session = self.env['windcave.session'].search([('session_id', '=', session_id)])
        

        retry_url = urls.url_join(self.env['ir.config_parameter'].sudo().get_param('web.base.url'), '/payment/windcave/callback?sessionId=%s&retries=%s'%(session_id, str(retries)))
        retry_url_increment = urls.url_join(self.env['ir.config_parameter'].sudo().get_param('web.base.url'), '/payment/windcave/callback?sessionId=%s&retries=%s'%(session_id, str(retries + 1)))

        _logger.info('Attempting to get lock.')
        filepath = os.path.join(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data/lock_files/%s.txt'%(session_id))
        got_lock = self._lock_file(filepath)

        if got_lock:
            _logger.info('Got lock. Session state: %s', session.session_state)

            if session.session_state == 'pending':
                session.update({'session_state' : 'processing'})
                _logger.info('Querying session')
                query_result = self._query_session(session_id, retries)
                result = query_result['result']
                _logger.info('Query session result: %s', result)
                if result == "OK":
                    transaction = self.env['windcave.transaction'].search([('reference', '=', session.reference)])
                    if transaction.check_transaction_set(session.order_id, 'purchase') or transaction.check_transaction_set(session.order_id, 'auth'):
                        _logger.info('Setting session: approved')
                        session.update({'session_state' : 'approved'})
                        if transaction.transaction_type == 'purchase':
                            self._set_transaction_done()
                        elif transaction.transaction_type == 'auth':
                            self._set_transaction_authorized()
                    else:
                        _logger.info('Setting session: declined')
                        session.update({'session_state' : 'declined'})
                        self._set_transaction_cancel()
                    self._unlock_file(filepath)
                    _logger.info('Lock released.')
                    return '/payment/process'
                elif result == "error":
                    _logger.info('Setting session: declined')
                    session.update({'session_state' : 'declined'})
                    self._set_transaction_cancel()
                    self.update({'state_message':'An error occurred during the transaction.'})
                    self._unlock_file(filepath)
                    _logger.info('Lock released. Redirecting to cart.')
                elif result == 'retry':
                    _logger.info('Setting session: pending')
                    session.update({'session_state' : 'pending'})
                    self._unlock_file(filepath)
                    _logger.info('Lock released. Retrying.')
                    return retry_url_increment
                elif result == 'pending':
                    _logger.info('Setting session: pending')
                    session.update({'session_state' : 'pending'})
                    self._unlock_file(filepath)
                    _logger.info('Lock released. Redirecting to home.')
                    return '/payment/process'
                elif result == 'void required':
                    _logger.info('Setting session: void required.')
                    session.update({'session_state':'declined'})
                    self._do_internal_void(query_result['transaction_id'])
                    self._unlock_file(filepath)
                    return '/payment/process'
            else:
                if session.session_state == 'processing':
                    time.sleep(5)
                    self._unlock_file(filepath)
                    _logger.info('Lock released. Retrying.')
                    return retry_url
            self._unlock_file(filepath)
            _logger.info('Lock released.')
            return '/payment/process'
        else:
            _logger.info('Failed to get lock.')
            time.sleep(5)
            _logger.info('Retrying')
            return retry_url
    
    def _sessionNotification(self, session_id, retries = 0):

        session = self.env['windcave.session'].search([('session_id', '=', session_id)])

        #get lock folder
        filepath = os.path.join(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data/lock_files/%s.txt'%(session_id))
        got_lock = self._lock_file(filepath)

        if got_lock:
            _logger.info('Got lock. Session state: %s', session.session_state)

            if session.session_state == 'pending' or session.session_state == 'processing':
                session.update({'session_state' : 'processing'})
                _logger.info('Querying session')
                query_result = self._query_session(session_id, retries)
                result = query_result['result']
                _logger.info('Query session result: %s', result)
                if result == "OK":
                    transaction = self.env['windcave.transaction'].search([('reference', '=', session.reference)])
                    if transaction.check_transaction_set(session.order_id, 'purchase') or transaction.check_transaction_set(session.order_id, 'auth'):
                        _logger.info('Setting session: approved')
                        session.update({'session_state' : 'approved'})
                        if transaction.transaction_type == 'purchase':
                            self._set_transaction_done()
                        elif transaction.transaction_type == 'auth':
                            self._set_transaction_authorized()
                    else:
                        _logger.info('Setting session: declined')
                        session.update({'session_state' : 'declined'})
                        self._set_transaction_cancel()
                elif result == "error":
                    _logger.info('Setting session: declined')
                    session.update({'session_state' : 'declined'})
                    self._set_transaction_cancel()
                    self.update({'state_message':'An error occurred during the transaction.'})
                elif result == 'retry':
                    _logger.info('Setting session: pending')
                    session.update({'session_state' : 'pending'})
                    self._unlock_file(filepath)
                    _logger.info('Lock released. Retrying.')
                elif result == 'pending':
                    _logger.info('Setting session: pending')
                    session.update({'session_state' : 'pending'})
                    _logger.info('Redirecting to home.')
                elif result == 'void required':
                    _logger.info('Setting session: void required.')
                    session.update({'session_state':'declined'})
                    self._do_internal_void(query_result['transaction_id'])
                    self._set_transaction_cancel()
            self._unlock_file(filepath)
            _logger.info('Lock released.')
        else:
            _logger.info('Failed to get lock.')
            time.sleep(5)
            _logger.info('Retrying')

    def _transactionNotification(self, transactionId):

        transaction = self.env['windcave.transaction'].search([('transaction_id', '=', transactionId)])

        _logger.info('Transaction status: %s', transaction.status)

        if transaction.status == 'pending':
            _logger.info('Querying transaction')
            transaction.do_query()

    def _query_session(self, session_id, retries):
        _logger.info('Starting session query. Session ID: %s. Retries: %s'%(session_id, retries))

        response = self._do_request(session_id)
        response_data = response.json()

        _logger.info('Response. Status Code: %s. Body: %s'%(response.status_code, response_data)) #parse

        if response.status_code == 200:
            transaction_data = _helper.get_transaction(response_data)
            approved = transaction_data['authorised']
            session = self.env['windcave.session'].search([('session_id', '=', session_id)])
            if approved:
                _logger.info('Transaction approved. Adding record.')
                self.env['windcave.transaction'].add_transaction(transaction_data, 'approved', session.reference)
                order = self.env['sale.order'].search([('name', '=', self.reference.split('-',1)[0])])
                if math.isclose(float(session.amount), order.amount_total) and session.currency == order.currency_id.name:
                    _logger.info('Transaction approved. Adding record.')
                    self.env['windcave.transaction'].add_transaction(transaction_data, 'approved', session.reference)
                    if response_data['storeCard'] and ('card' in transaction_data): #and save cards enabled
                        expiry = transaction_data['card']['dateExpiryMonth'] + '/' + transaction_data['card']['dateExpiryYear']
                        self.env['payment.token'].sudo().create({
                            'acquirer_id' : self.acquirer_id.id,
                            'partner_id' : session.customer_id,
                            'acquirer_ref' : transaction_data['card']['id'],
                            'verified' : True,
                            'name' : "%s Expiry: %s"%(transaction_data['card']['cardNumber'], expiry),
                            'windcave_card_id' : transaction_data['card']['id'],
                            'windcave_card_number' : transaction_data['card']['cardNumber'],
                            'windcave_card_expiration' : expiry
                        })
                else:
                    _logger.info('Session amount and currency do not match order amount and currency. Void required.')
                    return {'result':'void required', 'transaction_id':transaction_data['id']}
            else:
                _logger.info('Transaction declined. Adding record.')
                self.env['windcave.transaction'].add_transaction(transaction_data, 'declined', session.reference)
            return {'result':'OK'}
        elif response.status_code == 202:
            if retries < self._retry_limit:
                time.sleep(5)
                _logger.info('Session outcome unknown')
                return {'result':'retry'}
            else:
                _logger.error('Request failed. Session outcome unknown. Gave up after %s retries.', retries)
                return {'result':'pending'}
        elif 400 <= response.status_code < 500:
            error_list = _helper.parse_errors(response_data['errors'])
            _logger.error('Request failed. Details: %s', error_list)
            return {'result':'error'}
        elif  500 <= response.status_code < 600:
            if retries < self._retry_limit:
                time.sleep(5)
                _logger.info('Request failed.')
                return {'result':'retry'}
            else:
                _logger.error('Request failed. Session outcome unknown. Gave up after %s retries.', retries)
                return {'result':'error'}
        else:
            if retries < self._retry_limit:
                time.sleep(5)
                _logger.info('Request failed.')
                return {'result':'retry'}
            else:
                _logger.error('Request failed. Session outcome unknown. Gave up after %s retries.', retries)
                return {'result':'error'}
        return True

    def _do_request(self, session_id):
        auth_to_encode = (self.acquirer_id.windcave_api_user + ":" + self.acquirer_id.windcave_api_key).encode('ascii')
        authorization = base64.b64encode(auth_to_encode).decode('ascii')
        headers = {
            "Content-Type" : "application/json; charset=utf-8",
            "Authorization" : "Basic " + authorization,
            "X-Client-Version" : "Flectra: " + service.common.exp_version()['server_version'] + ", Extension: " + self.env.ref('base.module_payment_windcave').installed_version
        }
        response = requests.get(self.acquirer_id.windcave_api_url + '/v1/sessions/' + session_id, headers=headers)
        return response        
    
    def _do_internal_void(self, transaction_id):
        _logger.info('Starting internal void for transaction id %s', transaction_id)

        transaction = self.env['windcave.transaction'].search([('transaction_id', '=', transaction_id)])

        result = transaction.start_transaction(_helper.get_xid(self.env['ir.config_parameter'].sudo().get_param('web.base.url'), self.reference, 'void'), 'void', self.reference, self.amount, self.currency_id.name, 0)

        if result == True:
            if transaction.check_transaction_set(transaction.reference.split('-',1)[0], 'void'):
                _logger.info('Void approved.')
                transaction.update({'status' : 'voided'})
                self._set_transaction_cancel()
                self.update({'state_message':'Error - tried to pay for an order using an invalid payment session. This transaction was voided, please try again.'})
                return True
            else:
                _logger.info('Void declined.')
                return False
        elif result == False:
            _logger.error('Void falied: error occurred during the transaction.')
            self._set_transaction_cancel()
            self.update({'state_message':'Error - failed to void an invalid payment session.'})
            return False
        elif result == 'pending':
            _logger.error('Void outcome unknown.')
            self._set_transaction_cancel()
            self.update({'state_message':'Error - attempted to void an invalid payment session. Outcome unknown.'})
            return False

    def _lock_file(self, filepath):
        try:
            with open(filepath, 'x') as lockfile:
                lockfile.write(str(os.getpid()))
        except IOError:
            return False
        return True

    def _unlock_file(self, filepath):
        os.remove(filepath)

class PaymentTokenWindcave(models.Model):
    _inherit = 'payment.token'

    windcave_card_id = fields.Char('Windcave Card Id')
    windcave_card_number = fields.Char('Windcave Card Number')
    windcave_card_expiration = fields.Char('Windcave Card Expiration')

    @api.model
    def windcave_create(self, values):
        _logger.info('token create')
        return values