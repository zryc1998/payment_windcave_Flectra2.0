# -*- coding: utf-8 -*-

import logging

from flectra import _, api, fields, models

_logger = logging.getLogger(__name__)


class WindcaveSavedCard(models.Model):
    _name = 'windcave.saved_card'
    _description = 'Windcave Saved Card'

    card_id = fields.Char(name="Card Id")
    customer_id = fields.Char(name="Customer Id")
    order_id = fields.Char(name="Order Id")
    card_expiration = fields.Char(name="Card Expiration")