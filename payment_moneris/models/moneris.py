# -*- coding: utf-'8' "-*-"

import base64
try:
    import simplejson as json
except ImportError:
    import json
import logging
import urlparse
import werkzeug.urls
import urllib2

from openerp.addons.payment.models.payment_acquirer import ValidationError
from openerp.addons.payment_moneris.controllers.main import MonerisController
from openerp.osv import osv, fields
from openerp.tools.float_utils import float_compare

_logger = logging.getLogger(__name__)


class AcquirerMoneris(osv.Model):
    _inherit = 'payment.acquirer'

    def _get_moneris_urls(self, cr, uid, environment, context=None):
        """ Moneris URLS """
        if environment == 'prod':
            return {
                'moneris_form_url': 'https://www3.moneris.com/HPPDP/index.php',
                'moneris_auth_url': 'https://www3.moneris.com/HPPDP/verifyTxn.php',
            }
        else:
            return {
                'moneris_form_url': 'https://esqa.moneris.com/HPPDP/index.php',
                'moneris_auth_url': 'https://esqa.moneris.com/HPPDP/verifyTxn.php',
            }

    def _get_providers(self, cr, uid, context=None):
        providers = super(AcquirerMoneris, self)._get_providers(cr, uid, context=context)
        providers.append(['moneris', 'Moneris'])
        return providers

    _columns = {
        'moneris_email_account': fields.char('Moneris ps_store_id', required_if_provider='moneris'),
        'moneris_seller_account': fields.char(
            'Moneris hpp_key',
            help='The Merchant ID is used to ensure communications coming from Moneris are valid and secured.'),
        'moneris_use_ipn': fields.boolean('Use IPN', help='Moneris Instant Payment Notification'),
        # Server 2 server
        'moneris_api_enabled': fields.boolean('Use Rest API'),
        'moneris_api_username': fields.char('Rest API Username'),
        'moneris_api_password': fields.char('Rest API Password'),
        'moneris_api_access_token': fields.char('Access Token'),
        'moneris_api_access_token_validity': fields.datetime('Access Token Validity'),
    }

    _defaults = {
        'moneris_use_ipn': True,
        'fees_active': False,
        'fees_dom_fixed': 0.35,
        'fees_dom_var': 3.4,
        'fees_int_fixed': 0.35,
        'fees_int_var': 3.9,
        'moneris_api_enabled': False,
    }

    def _migrate_moneris_account(self, cr, uid, context=None):
        """ COMPLETE ME """
        cr.execute('SELECT id, paypal_account FROM res_company')
        res = cr.fetchall()
        for (company_id, company_moneris_account) in res:
            if company_moneris_account:
                company_moneris_ids = self.search(cr, uid, [('company_id', '=', company_id), ('name', '=', 'moneris')], limit=1, context=context)
                if company_moneris_ids:
                    self.write(cr, uid, company_moneris_ids, {'moneris_email_account': company_moneris_account}, context=context)
                else:
                    moneris_view = self.pool['ir.model.data'].get_object(cr, uid, 'payment_moneris', 'moneris_acquirer_button')
                    self.create(cr, uid, {
                        'name': 'moneris',
                        'moneris_email_account': company_moneris_account,
                        'view_template_id': moneris_view.id,
                    }, context=context)
        return True

    def moneris_compute_fees(self, cr, uid, id, amount, currency_id, country_id, context=None):
        """ Compute moneris fees.

            :param float amount: the amount to pay
            :param integer country_id: an ID of a res.country, or None. This is
                                       the customer's country, to be compared to
                                       the acquirer company country.
            :return float fees: computed fees
        """
        acquirer = self.browse(cr, uid, id, context=context)
        if not acquirer.fees_active:
            return 0.0
        country = self.pool['res.country'].browse(cr, uid, country_id, context=context)
        if country and acquirer.company_id.country_id.id == country.id:
            percentage = acquirer.fees_dom_var
            fixed = acquirer.fees_dom_fixed
        else:
            percentage = acquirer.fees_int_var
            fixed = acquirer.fees_int_fixed
        fees = (percentage / 100.0 * amount + fixed ) / (1 - percentage / 100.0)
        return fees

    def moneris_form_generate_values(self, cr, uid, id, partner_values, tx_values, context=None):
        base_url = self.pool['ir.config_parameter'].get_param(cr, uid, 'web.base.url')
        acquirer = self.browse(cr, uid, id, context=context)

        moneris_tx_values = dict(tx_values)
        moneris_tx_values.update({
            'cmd': '_xclick',
            'business': acquirer.moneris_email_account,
            'item_name': tx_values['reference'],
            'item_number': tx_values['reference'],
            'amount': tx_values['amount'],
            'currency_code': tx_values['currency'] and tx_values['currency'].name or '',
            'address1': partner_values['address'],
            'city': partner_values['city'],
            'country': partner_values['country'] and partner_values['country'].name or '',
            'state': partner_values['state'] and partner_values['state'].name or '',
            'email': partner_values['email'],
            'zip': partner_values['zip'],
            'first_name': partner_values['first_name'],
            'last_name': partner_values['last_name'],
            'return': '%s' % urlparse.urljoin(base_url, MonerisController._return_url),
            'notify_url': '%s' % urlparse.urljoin(base_url, MonerisController._notify_url),
            'cancel_return': '%s' % urlparse.urljoin(base_url, MonerisController._cancel_url),
        })
        if acquirer.fees_active:
            moneris_tx_values['handling'] = '%.2f' % moneris_tx_values.pop('fees', 0.0)
        if moneris_tx_values.get('return_url'):
            moneris_tx_values['custom'] = json.dumps({'return_url': '%s' % moneris_tx_values.pop('return_url')})
        return partner_values, moneris_tx_values

    def moneris_get_form_action_url(self, cr, uid, id, context=None):
        acquirer = self.browse(cr, uid, id, context=context)
        return self._get_moneris_urls(cr, uid, acquirer.environment, context=context)['moneris_form_url']

    def _moneris_s2s_get_access_token(self, cr, uid, ids, context=None):
        """
        Note: see # see http://stackoverflow.com/questions/2407126/python-urllib2-basic-auth-problem
        for explanation why we use Authorization header instead of urllib2
        password manager
        """
        res = dict.fromkeys(ids, False)
        parameters = werkzeug.url_encode({'grant_type': 'client_credentials'})

        for acquirer in self.browse(cr, uid, ids, context=context):
            tx_url = self._get_moneris_urls(cr, uid, acquirer.environment)['moneris_rest_url']
            request = urllib2.Request(tx_url, parameters)

            # add other headers (https://developer.moneris.com/webapps/developer/docs/integration/direct/make-your-first-call/)
            request.add_header('Accept', 'application/json')
            request.add_header('Accept-Language', 'en_US')

            # add authorization header
            base64string = base64.encodestring('%s:%s' % (
                acquirer.moneris_api_username,
                acquirer.moneris_api_password)
            ).replace('\n', '')
            request.add_header("Authorization", "Basic %s" % base64string)

            request = urllib2.urlopen(request)
            result = request.read()
            res[acquirer.id] = json.loads(result).get('access_token')
            request.close()
        return res


class TxMoneris(osv.Model):
    _inherit = 'payment.transaction'

    _columns = {
        'moneris_txn_id': fields.char('Transaction ID'),
        'moneris_txn_type': fields.char('Transaction type'),
    }

    # --------------------------------------------------
    # FORM RELATED METHODS
    # --------------------------------------------------

    def _moneris_form_get_tx_from_data(self, cr, uid, data, context=None):
        reference, txn_id = data.get('rvaroid'), data.get('txn_num')
        if not reference or not txn_id:
            error_msg = 'Moneris: received data with missing reference (%s) or txn_id (%s)' % (reference, txn_id)
            _logger.error(error_msg)
            raise ValidationError(error_msg)

        # find tx -> @TDENOTE use txn_id ?
        tx_ids = self.pool['payment.transaction'].search(cr, uid, [('reference', '=', reference)], context=context)
        if not tx_ids or len(tx_ids) > 1:
            error_msg = 'Moneris: received data for reference %s' % (reference)
            if not tx_ids:
                error_msg += '; no order found'
            else:
                error_msg += '; multiple order found'
            _logger.error(error_msg)
            raise ValidationError(error_msg)
        return self.browse(cr, uid, tx_ids[0], context=context)

    def _moneris_form_get_invalid_parameters(self, cr, uid, tx, data, context=None):
        invalid_parameters = []
        """
        if data.get('notify_version')[0] != '3.4':
            _logger.warning(
                'Received a notification from Moneris with version %s instead of 2.6. This could lead to issues when managing it.' %
                data.get('notify_version')
            )
        if data.get('test_ipn'):
            _logger.warning(
                'Received a notification from Moneris using sandbox'
            ),
        """
        # TODO: txn_id: shoudl be false at draft, set afterwards, and verified with txn details
        if tx.moneris_txn_id and data.get('txn_num') != tx.moneris_txn_id:
            invalid_parameters.append(('txn_num', data.get('txn_num'), tx.moneris_txn_id))
        if tx.acquirer_reference and data.get('response_order_id') != tx.acquirer_reference:
            invalid_parameters.append(('response_order_id', data.get('response_order_id'), tx.acquirer_reference))
        # check what is buyed
        if float_compare(float(data.get('charge_total', '0.0')), (tx.amount), 2) != 0:
            invalid_parameters.append(('charge_total', data.get('charge_total'), '%.2f' % tx.amount))
        """
        if data.get('mc_currency') != tx.currency_id.name:
            invalid_parameters.append(('mc_currency', data.get('mc_currency'), tx.currency_id.name))
        """
        """
        if 'handling_amount' in data and float_compare(float(data.get('handling_amount')), tx.fees, 2) != 0:
            invalid_parameters.append(('handling_amount', data.get('handling_amount'), tx.fees))
        """
        # check buyer
        """
        if tx.partner_reference and data.get('payer_id') != tx.partner_reference:
            invalid_parameters.append(('payer_id', data.get('payer_id'), tx.partner_reference))
        """
        # check seller
        if data.get('rvarid') != tx.acquirer_id.moneris_email_account:
            invalid_parameters.append(('rvarid', data.get('rvarid'), tx.acquirer_id.moneris_email_account))
        if data.get('rvarkey') != tx.acquirer_id.moneris_seller_account:
            invalid_parameters.append(('rvarkey', data.get('rvarkey'), tx.acquirer_id.moneris_seller_account))

        return invalid_parameters

    def _moneris_form_validate(self, cr, uid, tx, data, context=None):
        status = data.get('result')
        data = {
            'moneris_txn_id': data.get('txn_num'),
            'moneris_txn_type': data.get('trans_name'),
            'partner_reference': data.get('cardholder'),
            'acquirer_reference': data.get('response_order_id')
        }
        if status == '1':
            _logger.info('Validated Moneris payment for tx %s: set as done' % (tx.reference))
            data.update(state='done', date_validate=data.get('date_stamp', fields.datetime.now()))
            return tx.write(data)
        else:
            error = 'Received unrecognized status for Moneris payment %s: %s, set as error' % (tx.reference, status)
            _logger.info(error)
            data.update(state='error', state_message=error)
            return tx.write(data)

    # --------------------------------------------------
    # SERVER2SERVER RELATED METHODS
    # --------------------------------------------------

    def _moneris_try_url(self, request, tries=3, context=None):
        """ Try to contact Moneris. Due to some issues, internal service errors
        seem to be quite frequent. Several tries are done before considering
        the communication as failed.

         .. versionadded:: pre-v8 saas-3
         .. warning::

            Experimental code. You should not use it before OpenERP v8 official
            release.
        """
        done, res = False, None
        while (not done and tries):
            try:
                res = urllib2.urlopen(request)
                done = True
            except urllib2.HTTPError as e:
                res = e.read()
                e.close()
                if tries and res and json.loads(res)['name'] == 'INTERNAL_SERVICE_ERROR':
                    _logger.warning('Failed contacting Moneris, retrying (%s remaining)' % tries)
            tries = tries - 1
        if not res:
            pass
            # raise openerp.exceptions.
        result = res.read()
        res.close()
        return result

    def _moneris_s2s_send(self, cr, uid, values, cc_values, context=None):
        """
         .. versionadded:: pre-v8 saas-3
         .. warning::

            Experimental code. You should not use it before OpenERP v8 official
            release.
        """
        tx_id = self.create(cr, uid, values, context=context)
        tx = self.browse(cr, uid, tx_id, context=context)

        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer %s' % tx.acquirer_id._moneris_s2s_get_access_token()[tx.acquirer_id.id],
        }
        data = {
            'intent': 'sale',
            'transactions': [{
                'amount': {
                    'total': '%.2f' % tx.amount,
                    'currency': tx.currency_id.name,
                },
                'description': tx.reference,
            }]
        }
        if cc_values:
            data['payer'] = {
                'payment_method': 'credit_card',
                'funding_instruments': [{
                    'credit_card': {
                        'number': cc_values['number'],
                        'type': cc_values['brand'],
                        'expire_month': cc_values['expiry_mm'],
                        'expire_year': cc_values['expiry_yy'],
                        'cvv2': cc_values['cvc'],
                        'first_name': tx.partner_name,
                        'last_name': tx.partner_name,
                        'billing_address': {
                            'line1': tx.partner_address,
                            'city': tx.partner_city,
                            'country_code': tx.partner_country_id.code,
                            'postal_code': tx.partner_zip,
                        }
                    }
                }]
            }
        else:
            # TODO: complete redirect URLs
            data['redirect_urls'] = {
                # 'return_url': 'http://example.com/your_redirect_url/',
                # 'cancel_url': 'http://example.com/your_cancel_url/',
            },
            data['payer'] = {
                'payment_method': 'moneris',
            }
        data = json.dumps(data)

        request = urllib2.Request('https://api.sandbox.moneris.com/v1/payments/payment', data, headers)
        result = self._moneris_try_url(request, tries=3, context=context)
        return (tx_id, result)

    def _moneris_s2s_get_invalid_parameters(self, cr, uid, tx, data, context=None):
        """
         .. versionadded:: pre-v8 saas-3
         .. warning::

            Experimental code. You should not use it before OpenERP v8 official
            release.
        """
        invalid_parameters = []
        return invalid_parameters

    def _moneris_s2s_validate(self, cr, uid, tx, data, context=None):
        """
         .. versionadded:: pre-v8 saas-3
         .. warning::

            Experimental code. You should not use it before OpenERP v8 official
            release.
        """
        values = json.loads(data)
        status = values.get('state')
        if status in ['approved']:
            _logger.info('Validated Moneris s2s payment for tx %s: set as done' % (tx.reference))
            tx.write({
                'state': 'done',
                'date_validate': values.get('udpate_time', fields.datetime.now()),
                'moneris_txn_id': values['id'],
            })
            return True
        elif status in ['pending', 'expired']:
            _logger.info('Received notification for Moneris s2s payment %s: set as pending' % (tx.reference))
            tx.write({
                'state': 'pending',
                # 'state_message': data.get('pending_reason', ''),
                'moneris_txn_id': values['id'],
            })
            return True
        else:
            error = 'Received unrecognized status for Moneris s2s payment %s: %s, set as error' % (tx.reference, status)
            _logger.info(error)
            tx.write({
                'state': 'error',
                # 'state_message': error,
                'moneris_txn_id': values['id'],
            })
            return False

    def _moneris_s2s_get_tx_status(self, cr, uid, tx, context=None):
        """
         .. versionadded:: pre-v8 saas-3
         .. warning::

            Experimental code. You should not use it before OpenERP v8 official
            release.
        """
        # TDETODO: check tx.moneris_txn_id is set
        headers = {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer %s' % tx.acquirer_id._moneris_s2s_get_access_token()[tx.acquirer_id.id],
        }
        url = 'https://api.sandbox.moneris.com/v1/payments/payment/%s' % (tx.moneris_txn_id)
        request = urllib2.Request(url, headers=headers)
        data = self._moneris_try_url(request, tries=3, context=context)
        return self.s2s_feedback(cr, uid, tx.id, data, context=context)
