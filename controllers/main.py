# -*- coding: utf-8 -*-
import json
import logging
import pprint
import werkzeug
from werkzeug import urls
import requests

from flectra import http, _
from flectra.http import request, Response
from flectra.addons.portal.controllers.portal import CustomerPortal
from flectra.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager

_logger = logging.getLogger(__name__)

class WindcaveController(http.Controller):
    _callback_url = '/payment/windcave/callback'
    _notification_url = '/payment/windcave/notification'
    _redirect_handler_url = '/payment/windcave/redirect_handler'

    _retry_limit = 5

    @http.route('/payment/windcave/callback', type='http', auth='public')
    def windcave_callback(self, **post):
        _logger.info('Windcave Callback')

        post['type'] = 'callback'

        redirect_url = request.env['payment.transaction'].sudo().form_feedback(post, 'windcave')
        return werkzeug.utils.redirect(redirect_url)

    @http.route('/payment/windcave/notification', type='json', auth='public')
    def windcave_notification(self):
        _logger.info('Windcave Notification')

        data = json.loads(request.httprequest.data)

        request.env['payment.transaction'].sudo().form_feedback(data, 'windcave')

        if data.get('sessionId') != None:
            # If session notification
            session = request.env['windcave.session'].search([('session_id', '=', data['sessionId'])])

            if session.session_state == 'pending' or session.session_state == 'processing':
                _logger.info('Response: 400')
                return Response("Pending", status=400)
        elif data.get('id') != None:
            # If transaction notification
            transaction = request.env['windcave.transaction'].search([('transaction_id', '=', data['id'])])

            if transaction.status == 'pending':
                _logger.info('Response: 400')
                return Response("Pending", status=400)


        _logger.info('Response: 200')
        return Response("OK", status=200)


    @http.route('/payment/windcave/redirect_handler', type='http', auth="public", csrf=False)
    def windcave_redirect_handler(self, **kwargs):
        _logger.info('WindcaveController.windcave_redirect_handler()')
        payment_transaction = request.env['payment.transaction'].sudo().search([('reference','=',kwargs['reference'])])

        if kwargs['windcave_payment_exists']:
            if kwargs['windcave_result'] == 'approved':
                payment_transaction._set_transaction_done()
            elif kwargs['windcave_result'] == 'declined':
                payment_transaction._set_transaction_cancel()
            elif kwargs['windcave_result'] == 'error':
                payment_transaction._set_transaction_cancel()
                payment_transaction.update({'state_message':'Error while processing payment.'})
            _logger.info('Redirecting to /payment/process')
            return werkzeug.utils.redirect('/payment/process')

        _logger.info('Redirecting to ' + kwargs['windcave_redirect_url'])
        return werkzeug.utils.redirect(kwargs['windcave_redirect_url'])

class WindcaveAccount(CustomerPortal):

    def _prepare_home_portal_values(self, counters):
        _logger.info('WindcaveAccount._prepare_home_portal_values()')
        values = super()._prepare_home_portal_values(counters)

        acquirer = request.env['payment.acquirer'].search([('name', '=', 'Windcave')])
        partner = request.env.user.partner_id

        if 'windcave_card_count' in counters:
            values['windcave_card_count'] = request.env['payment.token'].search_count([('partner_id', '=', partner.id), ('acquirer_id', '=', acquirer.id)])

        return values

    @http.route(['/my/windcave-saved-cards', '/my/windcave-saved-cards/page/<int:page>'], type='http', auth="user", website=True)
    def windcave_saved_cards(self, page=1, **kw):
        _logger.info('WindcaveAccount.windcave_saved_cards()')
        values = self._prepare_portal_layout_values()

        acquirer = request.env['payment.acquirer'].search([('name', '=', 'Windcave')])
        partner = request.env.user.partner_id

        domain = [('partner_id', '=', partner.id), ('acquirer_id', '=', acquirer.id)]

        card_count = request.env['payment.token'].search_count(domain)

        pager = portal_pager(
            url="/my/windcave-saved-cards",
            total=card_count,
            page=page,
            step=self._items_per_page
        )

        cards = request.env['payment.token'].search(domain, order='create_date desc', limit=self._items_per_page, offset=pager['offset'])

        values.update({
            'cards': cards,
            'page_name': 'windcave-card',
            'pager': pager,
            'default_url': '/my/windcave-saved-cards'
        })

        return request.render("payment_windcave.portal_my_windcave_cards", values)

    @http.route('/my/windcave-saved-cards/<model("payment.token"):card>/delete', type='http', auth='user')
    def delete_windcave_card(self, card):
        _logger.info('WindcaveAccount.delete_windcave_card()')

        partner = request.env.user.partner_id

        if partner.id == card.partner_id.id:
            _logger.info('Deleting windcave card')
            card.unlink()
        else:
            _logger.info('Failed to delete windcave card, user id %s does not match %s', partner.id, card.partner_id.id)
            
        return werkzeug.utils.redirect('/my/windcave-saved-cards')
