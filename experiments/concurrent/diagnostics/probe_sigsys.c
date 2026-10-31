#define _GNU_SOURCE
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <stddef.h>
#include <stdio.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>
static void caught(int sig, siginfo_t *info, void *context) {
    char buf[128];
    int len=snprintf(buf,sizeof(buf),"EXPECTED_SIGSYS syscall=%d arch=%x\n",info->si_syscall,info->si_arch);
    write(1,buf,len);
    _exit(77);
}
int main(void) {
    struct sigaction action={0}; action.sa_sigaction=caught; action.sa_flags=SA_SIGINFO;
    sigaction(SIGSYS,&action,0);
    struct sock_filter insns[]={BPF_STMT(BPF_LD|BPF_W|BPF_ABS,offsetof(struct seccomp_data,nr)),
       BPF_JUMP(BPF_JMP|BPF_JEQ|BPF_K,SYS_getpid,0,1),
       BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_TRAP),BPF_STMT(BPF_RET|BPF_K,SECCOMP_RET_ALLOW)};
    struct sock_fprog program={sizeof(insns)/sizeof(insns[0]),insns};
    if(prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0)||prctl(PR_SET_SECCOMP,SECCOMP_MODE_FILTER,&program))return 2;
    syscall(SYS_getpid);
    return 3;
}
