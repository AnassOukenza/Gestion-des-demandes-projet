"""Interface d’affectation des employés à un compte portail HR Assistance."""

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class HrAssistanceAssignment(models.TransientModel):
    _name = 'hr.assistance.assignment'
    _description = 'HR Assistance Employee Assignment'

    assistant_id = fields.Many2one('res.users', string='HR Assistant', required=True)
    employee_ids = fields.Many2many('hr.employee', string='Managed Employees')
    available_employee_ids = fields.Many2many(
        'hr.employee', compute='_compute_available_employees', string='Available Employees',
    )

    @api.depends('assistant_id')
    def _compute_available_employees(self):
        for wizard in self:
            wizard.available_employee_ids = wizard.assistant_id.sudo().available_managed_employee_ids

    @api.onchange('assistant_id')
    def _onchange_assistant_id(self):
        self.employee_ids = self.assistant_id.sudo().managed_employee_ids

    def action_save(self):
        """Recontrôler les permissions et les affectations avant de remplacer la sélection.

        Le domaine du formulaire aide la saisie ; le contrôle à l’enregistrement et
        l’index SQL restent nécessaires pour couvrir les mises à jour concurrentes.
        """
        self.ensure_one()
        self.env['res.users']._check_assignment_permission()
        assistant = self.assistant_id.sudo()
        if not assistant.has_group('hr_requests.group_hr_request_hr_assistance'):
            raise ValidationError(self.env._('Please select an HR Assistance account.'))
        # Read again on save: the list may have changed since the form opened.
        others = self.env['res.users'].sudo().with_context(active_test=False).search([
            ('id', '!=', assistant.id),
            ('managed_employee_ids', 'in', self.employee_ids.ids),
        ])
        if others:
            raise ValidationError(self.env._('An employee is already assigned to another HR assistant.'))
        assistant.write({'managed_employee_ids': [(6, 0, self.employee_ids.ids)]})
        return {'type': 'ir.actions.act_window_close'}