# Captures

> **Domaine public — [CC0 1.0](../LICENSES/CC0-1.0.txt).** Ces captures sont des
> relevés de ce qu'un appareil dit sur son bus USB. Servez-vous.
>
> *Public domain (CC0 1.0) — recordings of what a device says on its USB bus.*

Déposer ici les fichiers **pcapng** exportés de Wireshark (usbmon sur l'hôte,
SSL 360 dans la VM).

## Convention de nommage

`NN-description.pcapng` — le `NN` suit la checklist du README racine :
`01-idle.pcapng`, `02-launch-ssl360.pcapng`, `04-monitor-volume.pcapng`, …

## Journal obligatoire

Chaque session de capture ajoute une entrée dans `JOURNAL.md` (même dossier).
Sans le journal, les captures sont inexploitables : c'est lui qui dit *quelle
action* correspond à *quel moment* du trafic.
