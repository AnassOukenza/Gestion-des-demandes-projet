"""Périmètres de gestion et affectation exclusive des employés à une assistance RH."""

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
from psycopg2.errors import UniqueViolation


class ResUsers(models.Model):
    _inherit = 'res.users'

    @api.model
    def _sync_request_group_labels(self):
        # Une ancienne traduction française peut survivre au changement du XML.
        """Mettre à jour les libellés traduits en conservant les identifiants XML historiques."""
        languages = self.env['res.lang'].search([]).mapped('code')
        for xmlid in ('group_hr_request_daiko_damos', 'rule_group_hr_request_daiko_damos'):
            record = self.env.ref('hr_requests.' + xmlid)
            for language in set(languages) | {'en_US'}:
                record.with_context(lang=language).write({'name': 'Gestion des demandes — Autres'})

    def _hr_request_management_company_ids(self):
        return self._hr_request_scope_company_ids(management_only=True)

    def _hr_request_scope_company_ids(self, management_only=False):
        """Limiter la gestion aux sociétés autorisées et au groupe exclusif du compte.

        Metraco est reconnu par son nom normalisé ; Autres couvre les autres noms.
        management_only est conservé pour compatibilité avec les appels existants,
        mais ne modifie actuellement pas le périmètre.
        """
        self.ensure_one()
        manage_metraco = self.has_group('hr_requests.group_hr_request_metraco')
        manage_other = self.has_group('hr_requests.group_hr_request_daiko_damos')
        if manage_metraco == manage_other or self.share or not self.active:
            return []
        # Autres couvre toute société autorisée dont le nom n’est pas Metraco.
        return self.sudo().company_ids.filtered(
            lambda company: (company.name.strip().casefold() == 'metraco') == manage_metraco
        ).ids

    def _hr_request_company_domain(self):
        """Fournir la frontière globale de société utilisée par les règles de demandes."""
        self.ensure_one()
        if self.has_group('hr_requests.group_hr_request_administration'):
            return []
        if (self.has_group('hr_requests.group_hr_request_metraco')
                or self.has_group('hr_requests.group_hr_request_daiko_damos')):
            return [('requester_employee_id.company_id', 'in', self._hr_request_scope_company_ids())]
        return []

    def _hr_request_draft_domain(self):
        """Garder les brouillons visibles uniquement par leur créateur."""
        self.ensure_one()
        # Chacun garde ses propres brouillons dans Mes demandes.
        return ['|', ('state', '!=', 'draft'), ('create_uid', '=', self.id)]

    @api.constrains('groups_id')
    def _check_request_management_group(self):
        metraco = self.env.ref('hr_requests.group_hr_request_metraco', raise_if_not_found=False)
        daiko_damos = self.env.ref('hr_requests.group_hr_request_daiko_damos', raise_if_not_found=False)
        if not metraco or not daiko_damos:
            return
        for user in self:
            if metraco in user.groups_id and daiko_damos in user.groups_id:
                raise ValidationError(self.env._(
                    'Select only one document management group: Metraco or Others.'
                ))

    managed_employee_ids = fields.Many2many(
        'hr.employee',
        'hr_assistance_employee_rel',
        'user_id',
        'employee_id',
        string='Managed Employees',
    )

    available_managed_employee_ids = fields.Many2many(
        'hr.employee', compute='_compute_available_managed_employees',
        string='Available Employees',
    )

    @api.depends('managed_employee_ids')
    def _compute_available_managed_employees(self):
        """Exclure les employés affectés ailleurs, y compris à un compte archivé."""
        for user in self:
            others = self.sudo().with_context(active_test=False).search([
                ('id', '!=', user._origin.id or 0),
            ]).mapped('managed_employee_ids')
            user.available_managed_employee_ids = self.env['hr.employee'].sudo().search([
                ('id', 'not in', others.ids),
            ])

    def init(self):
        """Garantir en base qu’un employé appartient à une seule affectation RH.

        Le contrôle préalable explique les doublons existants ; l’index unique
        protège aussi contre deux affectations concurrentes.
        """
        self.env.cr.execute('''
            SELECT employee_id FROM hr_assistance_employee_rel
            GROUP BY employee_id HAVING COUNT(DISTINCT user_id) > 1
        ''')
        if self.env.cr.fetchone():
            raise ValidationError(self.env._(
                'Some employees are assigned to multiple HR assistants. Remove duplicate assignments before upgrading.'
            ))
        self.env.cr.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS hr_assistance_employee_unique
            ON hr_assistance_employee_rel (employee_id)
        ''')

    def _check_assignment_permission(self):
        """Réserver les modifications d’affectation aux administrateurs autorisés."""
        if not (
                self.env.su
                or self.env.user.has_group('base.group_system')
                or self.env.user.has_group('hr_requests.group_hr_request_administration')
        ):
            raise AccessError(self.env._('Only request administrators can change employee assignments.'))

    @api.constrains('managed_employee_ids', 'groups_id')
    def _check_assistance_assignment(self):
        group = self.env.ref('hr_requests.group_hr_request_hr_assistance')
        for user in self:
            if user.managed_employee_ids and group not in user.groups_id:
                raise ValidationError(self.env._('Employees can only be assigned to an HR Assistance account.'))

    @api.model
    def _sync_hr_assistance_users(self):
        """Synchroniser les comptes HR Assistance lors du chargement des données XML."""
        assistance_group = self.env.ref(
            'hr_requests.group_hr_request_hr_assistance'
        )
        users = self.search([
            ('groups_id', 'in', assistance_group.ids)
        ])
        users._set_hr_assistance_portal()
        return True

    def _set_hr_assistance_portal(self):
        """Imposer le type portail aux assistants, sans rappeler cette surcharge de write()."""
        assistance_group = self.env.ref(
            'hr_requests.group_hr_request_hr_assistance'
        )
        portal_group = self.env.ref('base.group_portal')
        internal_group = self.env.ref('base.group_user')

        assistance_users = self.filtered(
            lambda user: assistance_group in user.groups_id
        )

        if assistance_users:
            super(ResUsers, assistance_users).write({
                'groups_id': [
                    (3, internal_group.id),
                    (4, portal_group.id),
                ]
            })

    @api.model_create_multi
    def create(self, vals_list):
        if any('managed_employee_ids' in vals for vals in vals_list):
            self._check_assignment_permission()
        try:
            with self.env.cr.savepoint():
                users = super().create(vals_list)
        except UniqueViolation as error:
            if error.diag.constraint_name != 'hr_assistance_employee_unique':
                raise
            raise ValidationError(self.env._('An employee is already assigned to another HR assistant.')) from error
        users._set_hr_assistance_portal()
        return users

    def write(self, vals):
        if 'managed_employee_ids' in vals:
            self._check_assignment_permission()
        try:
            with self.env.cr.savepoint():
                result = super().write(vals)
        except UniqueViolation as error:
            if error.diag.constraint_name != 'hr_assistance_employee_unique':
                raise
            raise ValidationError(self.env._('An employee is already assigned to another HR assistant.')) from error

        if 'groups_id' in vals:
            self._set_hr_assistance_portal()

        return result
