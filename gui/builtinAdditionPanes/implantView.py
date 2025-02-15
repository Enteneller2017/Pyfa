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
from gui.builtinMarketBrowser.events import ITEM_SELECTED
import gui.mainFrame
from gui.builtinViewColumns.state import State
from gui.utils.staticHelpers import DragDropHelper
from gui.contextMenu import ContextMenu
import gui.globalEvents as GE
from eos.const import ImplantLocation
from service.fit import Fit
from service.market import Market
import gui.fitCommands as cmd


class ImplantViewDrop(wx.DropTarget):
    def __init__(self, dropFn, *args, **kwargs):
        super(ImplantViewDrop, self).__init__(*args, **kwargs)
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


class ImplantView(wx.Panel):
    def __init__(self, parent):
        wx.Panel.__init__(self, parent, id=wx.ID_ANY, pos=wx.DefaultPosition, style=wx.TAB_TRAVERSAL)
        self.mainFrame = gui.mainFrame.MainFrame.getInstance()

        mainSizer = wx.BoxSizer(wx.VERTICAL)

        self.implantDisplay = ImplantDisplay(self)
        mainSizer.Add(self.implantDisplay, 1, wx.EXPAND, 0)

        radioSizer = wx.BoxSizer(wx.HORIZONTAL)
        radioSizer.AddStretchSpacer()
        self.rbFit = wx.RadioButton(self, id=wx.ID_ANY, label="Use Fit-specific Implants", style=wx.RB_GROUP)
        self.rbChar = wx.RadioButton(self, id=wx.ID_ANY, label="Use Character Implants")
        radioSizer.Add(self.rbFit, 0, wx.ALL, 5)
        radioSizer.Add(self.rbChar, 0, wx.ALL, 5)
        radioSizer.AddStretchSpacer()

        mainSizer.Add(radioSizer, 0, wx.EXPAND, 5)

        self.SetSizer(mainSizer)
        self.SetAutoLayout(True)

        self.Bind(wx.EVT_RADIOBUTTON, self.OnRadioSelect, self.rbFit)
        self.Bind(wx.EVT_RADIOBUTTON, self.OnRadioSelect, self.rbChar)
        self.mainFrame.Bind(GE.FIT_CHANGED, self.fitChanged)

    def fitChanged(self, event):
        sFit = Fit.getInstance()
        activeFitID = self.mainFrame.getActiveFit()
        fit = sFit.getFit(activeFitID)
        if fit:
            self.source = fit.implantSource
            if fit.implantSource == ImplantLocation.FIT:
                self.rbFit.SetValue(True)
            else:
                self.rbChar.SetValue(True)

        self.rbFit.Enable(fit is not None)
        self.rbChar.Enable(fit is not None)

        event.Skip()

    def OnRadioSelect(self, event):
        fitID = self.mainFrame.getActiveFit()
        if fitID is not None:
            self.mainFrame.command.Submit(cmd.GuiChangeImplantLocationCommand(
                fitID=fitID, source=ImplantLocation.FIT if self.rbFit.GetValue() else ImplantLocation.CHARACTER))


class ImplantDisplay(d.Display):
    DEFAULT_COLS = [
        "State",
        "attr:implantness",
        "Base Icon",
        "Base Name",
        "Price",
    ]

    def __init__(self, parent):
        d.Display.__init__(self, parent, style=wx.LC_SINGLE_SEL | wx.BORDER_NONE)

        self.lastFitId = None

        self.mainFrame.Bind(GE.FIT_CHANGED, self.fitChanged)
        self.mainFrame.Bind(ITEM_SELECTED, self.addItem)
        self.Bind(wx.EVT_LEFT_DCLICK, self.removeItem)
        self.Bind(wx.EVT_LEFT_DOWN, self.click)
        self.Bind(wx.EVT_KEY_UP, self.kbEvent)
        self.SetDropTarget(ImplantViewDrop(self.handleListDrag))

        self.Bind(wx.EVT_CONTEXT_MENU, self.spawnMenu)


    def handleListDrag(self, x, y, data):
        """
        Handles dragging of items from various pyfa displays which support it

        data is list with two indices:
            data[0] is hard-coded str of originating source
            data[1] is typeID or index of data we want to manipulate
        """

        if data[0] == "market":
            if self.mainFrame.command.Submit(cmd.GuiAddImplantCommand(self.mainFrame.getActiveFit(), int(data[1]))):
                self.mainFrame.additionsPane.select("Implants")

    def kbEvent(self, event):
        keycode = event.GetKeyCode()
        if keycode in (wx.WXK_DELETE, wx.WXK_NUMPAD_DELETE):
            row = self.GetFirstSelected()
            if row != -1:
                self.removeImplant(self.implants[self.GetItemData(row)])
        event.Skip()

    def fitChanged(self, event):
        sFit = Fit.getInstance()
        fit = sFit.getFit(event.fitID)

        self.Parent.Parent.Parent.DisablePage(self.Parent, not fit or fit.isStructure)

        # Clear list and get out if current fitId is None
        if event.fitID is None and self.lastFitId is not None:
            self.DeleteAllItems()
            self.lastFitId = None
            event.Skip()
            return

        self.original = fit.appliedImplants if fit is not None else None
        self.implants = fit.appliedImplants[:] if fit is not None else None
        if self.implants is not None:
            self.implants.sort(key=lambda implant: implant.slot or 0)

        if event.fitID != self.lastFitId:
            self.lastFitId = event.fitID

            item = self.GetNextItem(-1, wx.LIST_NEXT_ALL, wx.LIST_STATE_DONTCARE)

            if item != -1:
                self.EnsureVisible(item)

            self.deselectItems()

        self.update(self.implants)
        event.Skip()

    def addItem(self, event):
        item = Market.getInstance().getItem(event.itemID, eager='group.category')
        if item is None or not item.isImplant:
            event.Skip()
            return

        fitID = self.mainFrame.getActiveFit()
        fit = Fit.getInstance().getFit(fitID)

        if not fit or fit.isStructure:
            event.Skip()
            return

        self.mainFrame.command.Submit(cmd.GuiAddImplantCommand(fitID, event.itemID))
        # Select in any case - as we might've added implant which has been there already and command failed
        self.mainFrame.additionsPane.select('Implants')

        event.Skip()

    def removeItem(self, event):
        # Character implants can't be changed here...
        if self.Parent.source == ImplantLocation.CHARACTER:
            return

        row, _ = self.HitTest(event.Position)
        if row != -1:
            col = self.getColumn(event.Position)
            if col != self.getColIndex(State):
                self.removeImplant(self.implants[self.GetItemData(row)])

    def removeImplant(self, implant):
        fitID = self.mainFrame.getActiveFit()
        sFit = Fit.getInstance()
        fit = sFit.getFit(fitID)
        if fit.implantLocation == ImplantLocation.FIT:
            self.mainFrame.command.Submit(cmd.GuiRemoveImplantCommand(fitID, self.original.index(implant)))

    def click(self, event):
        event.Skip()

        # Character implants can't be changed here...
        if self.Parent.source == ImplantLocation.CHARACTER:
            return

        row, _ = self.HitTest(event.Position)
        if row != -1:
            col = self.getColumn(event.Position)
            if col == self.getColIndex(State):
                fitID = self.mainFrame.getActiveFit()
                implant = self.implants[self.GetItemData(row)]
                self.mainFrame.command.Submit(cmd.GuiToggleImplantStateCommand(fitID=fitID, position=self.original.index(implant)))

    def spawnMenu(self, event):
        sel = self.GetFirstSelected()
        menu = None

        sFit = Fit.getInstance()
        fit = sFit.getFit(self.mainFrame.getActiveFit())

        if not fit:
            return

        if sel != -1:
            implant = self.implants[sel]
            sMkt = Market.getInstance()
            sourceContext = "implantItem" if fit.implantSource == ImplantLocation.FIT else "implantItemChar"
            itemContext = sMkt.getCategoryByItem(implant.item).name

            menu = ContextMenu.getMenu((implant,), (sourceContext, itemContext))
        elif sel == -1 and fit.implantSource == ImplantLocation.FIT:
            fitID = self.mainFrame.getActiveFit()
            if fitID is None:
                return
            context = (("implantView",),)
            menu = ContextMenu.getMenu([], *context)
        if menu is not None:
            self.PopupMenu(menu)
