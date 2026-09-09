/* QML About dialog for Deb-Dev Modern Calamares Installer
   Adapted from KaOS branding templates
   SPDX-License-Identifier: GPL-3.0-or-later
*/
import io.calamares.core 1.0
import io.calamares.ui 1.0

import QtQuick 2.7
import QtQuick.Controls 2.0
import QtQuick.Layouts 1.3
import QtQuick.Window 2.3

ApplicationWindow {
    id: about
    visible: true
    width: 760
    height: 400
    title: qsTr("About Calamares")

    property var appName: "Calamares"
    property var appVersion: "3.3"

    Rectangle {
        id: textArea
        anchors.fill: parent
        color: "#1A1C1E"

        Column {
            id: column
            anchors.centerIn: parent

            Rectangle {
                width: 560
                height: 250
                radius: 10
                border.width: 1
                border.color: "#2A2D35"
                color: "#24292F"

                Text {
                    width: 400
                    height: 230
                    anchors.centerIn: parent
                    textFormat: Text.RichText
                    text: qsTr("<h1>%1</h1><br/>
                        <strong>%2<br/>
                        for %3</strong><br/><br/>
                        Copyright 2014-2017 Teo Mrnjavac &lt;teo@kde.org&gt;<br/>
                        Copyright 2017-2022 Adriaan de Groot &lt;groot&gt;<br/>
                        Thanks to <a href='https://calamares.io/team/' style='color: #47A7F5'>the Calamares team</a>
                        and the <a href=\"https://github.com/acoroslinux/deb-dev-builder/issues\" style='color: #47A7F5'>Deb-Dev Modern
                        team</a>.<br/><br/>
                        <a href='https://calamares.io/' style='color: #47A7F5'>Calamares</a>
                        development is sponsored by <br/>
                        <a href='http://www.blue-systems.com/' style='color: #47A7F5'>Blue Systems</a> -
                        Liberating Software." )
                        .arg(appName)
                        .arg(appVersion)
                        .arg(Branding.string(Branding.VersionedName))

                        onLinkActivated: Qt.openUrlExternally(link)

                        MouseArea {
                            anchors.fill: parent
                            acceptedButtons: Qt.NoButton
                            cursorShape: parent.hoveredLink ? Qt.PointingHandCursor : Qt.ArrowCursor
                        }

                    font.pointSize: 10
                    color: "#F0F0F0"
                    anchors.verticalCenterOffset: 10
                    anchors.horizontalCenterOffset: 40
                    wrapMode: Text.WordWrap
                }

                Image {
                    id: image
                    x: 16
                    y: 75
                    height: 100
                    fillMode: Image.PreserveAspectFit
                    source: "squid.png"
                }
            }
        }

        Button {
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottom: parent.bottom
            anchors.bottomMargin: 15
            text: qsTr("Close")
            hoverEnabled: true
            onClicked: about.close();
        }
    }
}
