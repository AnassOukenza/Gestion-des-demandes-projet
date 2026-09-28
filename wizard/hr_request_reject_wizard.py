"""Saisie du motif de rejet avant délégation au workflow de la demande."""

from odoo import fields, models
from odoo.exceptions import UserError


class HrRequestRejectWizard(models.TransientModel):
    _name = 'hr.request.reject.wizard'
    _description = 'Reject Request'

    request_id = fields.Many2one('hr.request', string='Reference', required=True, readonly=True)
    reason_code = fields.Selection([
        ('missing_information', 'Informations manquantes'),
        ('incorrect_information', 'Informations incorrectes'),
        ('wrong_type', 'Type de demande incorrect'),
        ('duplicate', 'Demande en double'),
        ('not_eligible', 'Demande non éligible'),
        ('other', 'Autre'),
    ], string='Rejection Reason', required=True)
    reason = fields.Text(string='Specify the reason')

    def action_confirm(self):
        """Résoudre le motif choisi puis déléguer contrôles et transition à la demande."""
        self.ensure_one()
        self.check_access('write')
        if self.reason_code == 'other':
            reason = (self.reason or '').strip()
            if not reason:
                raise UserError(self.env._('Please specify the rejection reason.'))
        else:
            reasons = dict(self._fields['reason_code'].selection)
            reason = reasons.get(self.reason_code)
            if not reason:
                raise UserError(self.env._('Please select a rejection reason.'))
        self.request_id._reject_with_reason(reason)
        return {'type': 'ir.actions.act_window_close'}