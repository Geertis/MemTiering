/* SPDX-License-Identifier: GPL-2.0 */
#ifndef DEMETER_SANDBOX_H
#define DEMETER_SANDBOX_H
#include <linux/kobject.h>
int sandbox_sysfs_init(struct kobject *root);
void sandbox_sysfs_exit(void);
bool sandbox_enabled(void);
bool demeter_legacy_enabled(void);
#endif
