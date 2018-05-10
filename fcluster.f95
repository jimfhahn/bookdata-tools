subroutine compute_clusters(nc, clusters, ne, ls, rs)
  implicit none
  integer nc, ne
  integer clusters(nc)
  integer ls(ne), rs(ne)
  integer left, right

  integer nchanged
  integer iter
  integer i
  
  iter = 0

  do while (nchanged > 0)
    nchanged = 0
    iter = iter + 1
    do i=1,ne
      left = ls(i)
      right = rs(i)
      if (clusters(left) < clusters(right)) then
        clusters(right) = clusters(left)
        nchanged = nchanged + 1
      end if
    end do
  end do

end
