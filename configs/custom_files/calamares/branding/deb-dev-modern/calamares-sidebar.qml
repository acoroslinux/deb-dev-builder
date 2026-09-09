/* QML progress bar for Deb-Dev Modern Calamares Installer
   Adapted from KaOS branding templates
   SPDX-License-Identifier: GPL-3.0-or-later
*/
import io.calamares.ui 1.0
import io.calamares.core 1.0

import QtQuick 2.3
import QtQuick.Layouts 1.3
import QtQuick.Controls 2.15

Rectangle {
    id: sideBar;
    color: Branding.styleString( Branding.SidebarBackground );
    height: 48;
    width: parent.width

    RowLayout {
        anchors.fill: parent;
        spacing: 2;

        Item {
            Layout.fillHeight: true;
        }

        Repeater {
            model: ViewManager
            Rectangle {
                Layout.leftMargin: 0;
                Layout.fillWidth: true;
                Layout.alignment: Qt.AlignTop;
                height: 45;
                radius: 0;
                color: Branding.styleString( index == ViewManager.currentStepIndex ? Branding.SidebarBackgroundCurrent : Branding.SidebarBackground );

                Text {
                    anchors.verticalCenter: parent.verticalCenter;
                    anchors.horizontalCenter: parent.horizontalCenter;
                    color: Branding.styleString( index == ViewManager.currentStepIndex ? Branding.SidebarTextCurrent : Branding.SidebarText );
                    text: display;
                    font.pointSize : index == ViewManager.currentStepIndex ? 10 : 9;
                    font.bold: index == ViewManager.currentStepIndex;
                }

                Rectangle {
                    height: 3;
                    anchors.left: parent.left;
                    anchors.right: parent.right;
                    anchors.bottom: parent.bottom;
                    color: Branding.styleString(ViewManager.currentStepIndex === index ? Branding.SidebarTextCurrent : (ViewManager.currentStepIndex > index ? Branding.SidebarTextCurrent : Branding.SidebarBackgroundCurrent));

                    Image {
                        source: "pan-up-symbolic.svg"
                        id: image
                        anchors.horizontalCenter: parent.horizontalCenter;
                        anchors.bottom: parent.top;
                        anchors.bottomMargin: -2;
                        fillMode: Image.PreserveAspectFit;
                        height: 8;
                        visible: index == ViewManager.currentStepIndex;
                    }
                }
            }
        }
    }
}
