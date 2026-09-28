"""Adaptation des menus de gestion pour les comptes d’administration des demandes."""

from odoo import api, models


class IrUiMenu(models.Model):
    _inherit = 'ir.ui.menu'

    @api.model
    def _visible_menu_ids(self, debug=False):
        # Ne pas modifier l’ensemble mis en cache par Odoo pour les autres comptes.
        """Masquer le menu de traitement pour l’administration sur une copie du résultat.

        La visibilité du menu est une règle de présentation ; les droits restent
        contrôlés par les ACL, les règles d’enregistrement et les actions métier.
        """
        visible = set(super()._visible_menu_ids(debug=debug))
        administration = self.env.ref(
            'hr_requests.group_hr_request_administration', raise_if_not_found=False,
        )
        if administration and administration in self.env.user.groups_id:
            manage_menu = self.env.ref('hr_requests.menu_hr_request_manage', raise_if_not_found=False)
            if manage_menu:
                visible.discard(manage_menu.id)
        return visible
