# =============================================================================
# Copyright (C) 2010 Diego Duclos
#
# This file is part of pyfa.
#
# pyfa is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pyfa is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with pyfa.  If not, see <http://www.gnu.org/licenses/>.
# =============================================================================

# noinspection PyPackageRequirements
import wx
import gui.display as d
from gui.builtinViewColumns.state import State
from gui.contextMenu import ContextMenu
import gui.globalEvents as GE
from gui.utils.staticHelpers import DragDropHelper
from service.fit import Fit
from service.market import Market
import gui.fitCommands as cmd


class CargoViewDrop(wx.DropTarget):
    def __init__(self, dropFn, *args, **kwargs):
        super(CargoViewDrop, self).__init__(*args, **kwargs)
        self.dropFn = dropFn
        # this is really transferring an EVE itemID
        self.dropData = wx.TextDataObject()
        self.SetDataObject(self.dropData)

    def OnData(self, x, y, t):
        if self.GetData():
            dragged_data = DragDropHelper.data
            data = dragged_data.split(':')
            self.dropFn(x, y, data)
        return t


# @todo: Was copied form another class and modified. Look through entire file, refine
class CargoView(d.Display):
    DEFAULT_COLS = ["Base Icon",
                    "Base Name",
                    "attr:volume",
                    "Price"]

    def __init__(self, parent):
        d.Display.__init__(self, parent, style=wx.LC_SINGLE_SEL | wx.BORDER_NONE)

        self.lastFitId = None

        self.mainFrame.Bind(GE.FIT_CHANGED, self.fitChanged)
        self.Bind(wx.EVT_LEFT_DCLICK, self.removeItem)
        self.Bind(wx.EVT_KEY_UP, self.kbEvent)

        self.SetDropTarget(CargoViewDrop(self.handleListDrag))
        self.Bind(wx.EVT_LIST_BEGIN_DRAG, self.startDrag)

        self.Bind(wx.EVT_CONTEXT_MENU, self.spawnMenu)

    def handleListDrag(self, x, y, data):
        """
        Handles dragging of items from various pyfa displays which support it

        data is list with two indices:
            data[0] is hard-coded str of originating source
            data[1] is typeID or index of data we want to manipulate
        """

        if data[0] == "fitting":
            self.swapModule(x, y, int(data[1]))
        elif data[0] == "market":
            fit = self.mainFrame.getActiveFit()
            if fit:
                self.mainFrame.command.Submit(cmd.GuiAddCargoCommand(fit, int(data[1]), 1))

    def startDrag(self, event):
        row = event.GetIndex()

        if row != -1:
            data = wx.TextDataObject()
            dataStr = "cargo:{}".format(self.cargo[row].itemID)
            data.SetText(dataStr)

            dropSource = wx.DropSource(self)
            dropSource.SetData(data)
            DragDropHelper.data = dataStr
            dropSource.DoDragDrop()

    def kbEvent(self, event):
        keycode = event.GetKeyCode()
        if keycode in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE):
            row = self.GetFirstSelected()
            if row != -1:
                fitID = self.mainFrame.getActiveFit()
                cargo = self.cargo[self.GetItemData(row)]
                self.mainFrame.command.Submit(cmd.GuiRemoveCargoCommand(fitID=fitID, itemID=cargo.itemID))
        event.Skip()

    def swapModule(self, x, y, modIdx):
        """Swap a module from fitting window with cargo"""
        sFit = Fit.getInstance()
        fit = sFit.getFit(self.mainFrame.getActiveFit())
        dstRow, _ = self.HitTest((x, y))
        mstate = wx.GetMouseState()

        self.mainFrame.command.Submit(cmd.GuiLocalModuleToCargoCommand(
            fitID=self.mainFrame.getActiveFit(),
            modPosition=fit.modules[modIdx].modPosition,
            cargoItemID=self.cargo[dstRow].itemID if dstRow > -1 else None,
            copy=mstate.cmdDown))

    def fitChanged(self, event):
        sFit = Fit.getInstance()
        fit = sFit.getFit(event.fitID)

        # self.Parent.Parent.DisablePage(self, not fit or fit.isStructure)

        # Clear list and get out if current fitId is None
        if event.fitID is None and self.lastFitId is not None:
            self.DeleteAllItems()
            self.lastFitId = None
            event.Skip()
            return

        self.original = fit.cargo if fit is not None else None
        self.cargo = fit.cargo[:] if fit is not None else None
        if self.cargo is not None:
            self.cargo.sort(key=lambda c: (c.item.group.category.name, c.item.group.name, c.item.name))

        if event.fitID != self.lastFitId:
            self.lastFitId = event.fitID

            item = self.GetNextItem(-1, wx.LIST_NEXT_ALL, wx.LIST_STATE_DONTCARE)

            if item != -1:
                self.EnsureVisible(item)

            self.deselectItems()

        self.populate(self.cargo)
        self.refresh(self.cargo)
        event.Skip()

    def removeItem(self, event):
        row, _ = self.HitTest(event.Position)
        if row != -1:
            col = self.getColumn(event.Position)
            if col != self.getColIndex(State):
                fitID = self.mainFrame.getActiveFit()
                cargo = self.cargo[self.GetItemData(row)]
                self.mainFrame.command.Submit(cmd.GuiRemoveCargoCommand(fitID, cargo.itemID))

    def spawnMenu(self, event):
        sel = self.GetFirstSelected()
        if sel != -1:
            cargo = self.cargo[sel]
            sMkt = Market.getInstance()
            sourceContext = "cargoItem"
            itemContext = sMkt.getCategoryByItem(cargo.item).name

            menu = ContextMenu.getMenu((cargo,), (sourceContext, itemContext))
            self.PopupMenu(menu)
